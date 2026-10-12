//! Naga IR 静态分析 —— 防爆门禁的实质部分。
//!
//! naga 的前端只负责「这段 GLSL 合不合语法、类型对不对、readonly 有没有被写」。
//! 它**不**拦截死循环: `while(true)`、`for(;;)`、`do{}while(true)` 全部能通过
//! parse + validate。真机上这类算子会把合成线程挂死, 整台设备黑屏。
//!
//! 所以循环有界性必须我们自己证。这里做的是一个保守的**归纳变量分析**:
//! 只有能同时给出「常量初值 + 常量步长 + 常量上界」三件套的循环才放行,
//! 任何证不出来的循环 (包括上界来自 `imageSize()` 这类运行期值的) 一律打回。
//! 保守的方向是对的 —— 门禁宁可错杀一个能跑的算子, 不可放过一个挂死真机的算子。

use naga::{Block, Expression, Function, Handle, Literal, Module, Statement};

/// 单个循环的分析结论。
#[derive(Debug, Clone, PartialEq)]
pub enum LoopBound {
    /// 证明了有限上界 (最大迭代次数)。
    Proven(u64),
    /// 证不出来。附上人类可读的原因, 直接作为 LLM 的下一轮反馈。
    Unproven(String),
}

/// 静态提取的算子特征。既用于成本模型, 也回灌给 LLM 当反馈。
#[derive(Debug, Clone, Default, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct ShaderFeatures {
    /// 显存纹理读次数 (已按所在循环的静态迭代上界展开)。
    pub image_loads: u32,
    /// 显存纹理写次数 (同上)。
    pub image_stores: u32,
    /// `imageSize()` 之类的查询次数。
    pub image_queries: u32,
    /// 普通 ALU 指令数 (加减乘、比较、取反……, 已展开)。
    pub alu_ops: u32,
    /// 超越函数 / 高开销内建 (exp/log/sin/pow/sqrt/inverseSqrt……, 已展开)。
    pub transcendental_ops: u32,
    /// 分支数量 (已展开)。
    pub branches: u32,
    /// 循环个数 (未展开, 只数静态条数)。
    pub loops: u32,
    /// 所有循环静态迭代上界的最大值。
    pub max_loop_trip: u64,
    /// 片上 shared/LDS 字节数。
    pub workgroup_bytes: u32,
    /// 工作组尺寸。
    pub workgroup_size: [u32; 3],
    /// SPIR-V 字数 (由 validator 填)。
    pub spirv_words: u32,
    /// 被 imageLoad 真正读过的全局变量下标。
    ///
    /// 用来判「声明了却没读」: 插帧算子若只抄当前帧, binding 契约全满足、
    /// 耗时还特别漂亮, 但它根本没在插帧。
    pub images_read: Vec<u32>,
}

impl ShaderFeatures {
    /// 每个输出像素的总访存次数 —— 移动端 TBDR 上真正的瓶颈量。
    pub fn memory_ops(&self) -> u32 {
        self.image_loads + self.image_stores
    }

    pub fn workgroup_invocations(&self) -> u32 {
        self.workgroup_size[0]
            .saturating_mul(self.workgroup_size[1])
            .saturating_mul(self.workgroup_size[2])
    }
}

/// 一次 IR 走查的全部产出。
#[derive(Debug, Clone)]
pub struct IrAnalysis {
    pub features: ShaderFeatures,
    /// 每个循环的定界结论, 按遍历顺序。
    pub loop_bounds: Vec<LoopBound>,
    /// 出现在非一致控制流 (if/loop/switch 内部) 里的 barrier —— 真机上会挂。
    pub divergent_barriers: u32,
    /// 是否每一条 ImageStore 都被 imageSize 派生的条件保护住了。
    pub all_stores_guarded: bool,
    /// 未被保护的 ImageStore 条数。
    pub unguarded_stores: u32,
}

impl IrAnalysis {
    /// 证不出界的循环理由清单。
    pub fn unproven_loops(&self) -> Vec<&str> {
        self.loop_bounds
            .iter()
            .filter_map(|b| match b {
                LoopBound::Unproven(why) => Some(why.as_str()),
                LoopBound::Proven(_) => None,
            })
            .collect()
    }
}

/// 单次走查的可变状态。
struct Walker<'a> {
    module: &'a Module,
    func: &'a Function,
    features: ShaderFeatures,
    loop_bounds: Vec<LoopBound>,
    divergent_barriers: u32,
    unguarded_stores: u32,
    /// 当前调用栈上的函数, 用于防御性地挡住环 (GLSL 本身禁止递归)。
    call_stack: Vec<usize>,
    /// 是否在统计开销 (Pass B)。Pass A 只做安全检查, 不数指令。
    counting: bool,
    /// 当前语句嵌套在几层 if/loop/switch 里。0 = 一致控制流。
    control_depth: u32,
    /// 局部变量 → 所有被存入它的值表达式。
    ///
    /// GLSL 的 `||` / `&&` 是短路求值, naga 会把
    /// `if (p.x >= o.x || p.y >= o.y) return;` 降成「算一半 → 存临时 bool →
    /// 再按需算另一半 → 读临时 bool 做分支」。于是那条 `If` 的 condition
    /// 只是一个 `Load(临时局部)`, 表达式树里根本看不见 `imageSize`。
    /// 要判断守卫是不是从输出尺寸推出来的, 就必须能顺着局部变量的定值链往回走。
    local_defs: Vec<Vec<Handle<Expression>>>,
    /// 走到当前位置时, 各局部变量的已知编译期常量值。
    /// `None` = 曾被非常量写入, 不可用于定界。
    const_env: Vec<Option<i64>>,
}

/// 走查上下文: 沿着块结构往下传的信息。
#[derive(Clone, Copy)]
struct Ctx {
    /// 当前位置的静态迭代倍率 (外层所有循环上界之积), 用于展开计数。
    repeat: u64,
    /// 当前位置是否已被「由 imageSize 派生的条件」保护。
    guarded: bool,
}

impl Ctx {
    fn root() -> Self {
        Ctx {
            repeat: 1,
            guarded: false,
        }
    }
    fn with_repeat(self, factor: u64) -> Self {
        Ctx {
            repeat: self.repeat.saturating_mul(factor).min(1 << 20),
            ..self
        }
    }
    fn guarded(self) -> Self {
        Ctx {
            guarded: true,
            ..self
        }
    }
}

/// 对整个 module 做一次走查。
///
/// 分两趟, 因为两件事的正确口径不一样:
///
/// **Pass A — 安全检查 (逐函数, 各走一遍)**
/// 循环有界性与 barrier 一致性是函数的局部性质, 而且**没被调用的函数也要查**:
/// 死代码里的死循环今天不执行, 明天被接上就挂机。
///
/// **Pass B — 开销统计与写回守卫 (从 entry point 出发, 穿过函数调用)**
/// 这一趟必须跟着调用走。实测中大模型很爱把采样抽成
/// `vec4 fetchTexel(...) { return imageLoad(...); }` 再在 4x4 循环里调 ——
/// 逐函数各算一遍的话, 这个算子的 21 次访存会被记成 1 次, 成本模型直接低估 21 倍。
/// 所以调用点要带着所在循环的迭代倍率递归进被调函数。
/// 边界守卫同理: 守卫在 main 里, imageStore 可能在被调函数里, 守卫状态要能穿过调用点。
pub fn analyse(module: &Module) -> IrAnalysis {
    let mut features = ShaderFeatures::default();
    if let Some(ep) = module.entry_points.first() {
        features.workgroup_size = ep.workgroup_size;
    }
    features.workgroup_bytes = workgroup_bytes(module);

    // ---- Pass A: 逐函数安全检查 ----
    let mut loop_bounds = Vec::new();
    let mut divergent_barriers = 0u32;

    let mut all_funcs: Vec<&Function> = module.functions.iter().map(|(_, f)| f).collect();
    for ep in &module.entry_points {
        all_funcs.push(&ep.function);
    }
    for func in all_funcs {
        let mut w = Walker::new(module, func, false);
        w.loop_bounds = std::mem::take(&mut loop_bounds);
        w.divergent_barriers = divergent_barriers;
        w.walk_block(&func.body, Ctx::root());
        loop_bounds = w.loop_bounds;
        divergent_barriers = w.divergent_barriers;
    }

    // ---- Pass B: 从 entry point 出发, 穿过调用统计 ----
    let mut unguarded_stores = 0u32;
    if let Some(ep) = module.entry_points.first() {
        let mut w = Walker::new(module, &ep.function, true);
        w.features = features;
        w.walk_block(&ep.function.body, Ctx::root());
        features = w.features;
        unguarded_stores = w.unguarded_stores;
    }

    features.loops = loop_bounds.len() as u32;
    features.max_loop_trip = loop_bounds
        .iter()
        .map(|b| match b {
            LoopBound::Proven(n) => *n,
            LoopBound::Unproven(_) => u64::MAX,
        })
        .max()
        .unwrap_or(0);

    IrAnalysis {
        features,
        loop_bounds,
        divergent_barriers,
        all_stores_guarded: unguarded_stores == 0,
        unguarded_stores,
    }
}

/// 预扫一遍函数体, 记下每个局部变量被写入过哪些值表达式。
fn collect_local_defs(func: &Function) -> Vec<Vec<Handle<Expression>>> {
    let mut defs = vec![Vec::new(); func.local_variables.len()];
    collect_in_block(func, &func.body, &mut defs);
    defs
}

fn collect_in_block(func: &Function, block: &Block, defs: &mut Vec<Vec<Handle<Expression>>>) {
    for stmt in block.iter() {
        match stmt {
            Statement::Store { pointer, value } => {
                if let Some(v) = base_local(func, *pointer) {
                    if let Some(slot) = defs.get_mut(v.index()) {
                        slot.push(*value);
                    }
                }
            }
            Statement::Block(inner) => collect_in_block(func, inner, defs),
            Statement::If { accept, reject, .. } => {
                collect_in_block(func, accept, defs);
                collect_in_block(func, reject, defs);
            }
            Statement::Switch { cases, .. } => {
                for c in cases {
                    collect_in_block(func, &c.body, defs);
                }
            }
            Statement::Loop {
                body, continuing, ..
            } => {
                collect_in_block(func, body, defs);
                collect_in_block(func, continuing, defs);
            }
            _ => {}
        }
    }
}

/// 剥掉 Access / AccessIndex, 找到指针最终指向的局部变量。
fn base_local(
    func: &Function,
    mut handle: Handle<Expression>,
) -> Option<Handle<naga::LocalVariable>> {
    for _ in 0..32 {
        match func.expressions.try_get(handle).ok()? {
            Expression::LocalVariable(v) => return Some(*v),
            Expression::Access { base, .. } | Expression::AccessIndex { base, .. } => {
                handle = *base;
            }
            _ => return None,
        }
    }
    None
}

/// 片上 shared 变量总字节数。
fn workgroup_bytes(module: &Module) -> u32 {
    module
        .global_variables
        .iter()
        .filter(|(_, g)| g.space == naga::AddressSpace::WorkGroup)
        .map(|(_, g)| type_size(module, g.ty))
        .sum()
}

fn type_size(module: &Module, ty: Handle<naga::Type>) -> u32 {
    use naga::TypeInner;
    match &module.types[ty].inner {
        TypeInner::Scalar(s) => s.width as u32,
        TypeInner::Vector { size, scalar } => *size as u32 * scalar.width as u32,
        TypeInner::Matrix {
            columns,
            rows,
            scalar,
        } => *columns as u32 * *rows as u32 * scalar.width as u32,
        TypeInner::Array { base, size, .. } => {
            let n = match size {
                naga::ArraySize::Constant(n) => n.get(),
                _ => 1,
            };
            type_size(module, *base).saturating_mul(n)
        }
        TypeInner::Struct { members, .. } => members.iter().map(|m| type_size(module, m.ty)).sum(),
        _ => 0,
    }
}

impl<'a> Walker<'a> {
    fn new(module: &'a Module, func: &'a Function, counting: bool) -> Self {
        let mut w = Walker {
            module,
            func,
            features: ShaderFeatures::default(),
            loop_bounds: Vec::new(),
            divergent_barriers: 0,
            unguarded_stores: 0,
            call_stack: Vec::new(),
            counting,
            control_depth: 0,
            local_defs: collect_local_defs(func),
            const_env: Vec::new(),
        };
        w.const_env = w.seed_const_env();
        w
    }

    /// 递归进被调函数, 带着调用点的迭代倍率与守卫状态。
    fn walk_call(&mut self, callee: Handle<naga::Function>, ctx: Ctx) {
        let idx = callee.index();
        // GLSL 禁止递归, 但 IR 是别人给的, 不替它打包票。
        if self.call_stack.contains(&idx) || self.call_stack.len() > 32 {
            return;
        }
        let Ok(func) = self.module.functions.try_get(callee) else {
            return;
        };

        let saved_func = self.func;
        let saved_defs = std::mem::replace(&mut self.local_defs, collect_local_defs(func));
        let saved_env = std::mem::take(&mut self.const_env);

        self.func = func;
        self.const_env = self.seed_const_env();
        self.call_stack.push(idx);

        self.walk_block(&func.body, ctx);

        self.call_stack.pop();
        self.func = saved_func;
        self.local_defs = saved_defs;
        self.const_env = saved_env;
    }

    /// 块内顺序走查。
    ///
    /// 保护状态会沿着语句序列**向后**传播: 一旦遇到
    /// `if (由 imageSize 派生的条件) { return; }` 这种早退守卫,
    /// 该块内其后的所有语句 (含嵌套) 都视为已受保护。
    fn walk_block(&mut self, block: &Block, ctx: Ctx) {
        let mut ctx = ctx;
        for stmt in block.iter() {
            self.walk_stmt(stmt, ctx);
            if let Statement::If {
                condition,
                accept,
                reject,
            } = stmt
            {
                if self.expr_reaches_image_size(*condition)
                    && (block_always_returns(accept) || block_always_returns(reject))
                {
                    ctx = ctx.guarded();
                }
            }
        }
    }

    fn walk_stmt(&mut self, stmt: &Statement, ctx: Ctx) {
        match stmt {
            Statement::Emit(range) => {
                // Emit 声明了一段被物化的表达式区间, 正好是数 ALU 的锚点。
                for handle in range.clone() {
                    self.count_expr(handle, ctx.repeat);
                }
            }
            Statement::Block(inner) => self.walk_block(inner, ctx),

            Statement::If {
                condition,
                accept,
                reject,
            } => {
                self.features.branches = self
                    .features
                    .branches
                    .saturating_add(ctx.repeat.min(u32::MAX as u64) as u32);

                // 条件本身从 imageSize 推出来 → 分支体内的写回是受保护的
                // (`if (p.x < sz.x && p.y < sz.y) { imageStore(...) }` 这种写法)。
                let branch_ctx = if self.expr_reaches_image_size(*condition) {
                    ctx.guarded()
                } else {
                    ctx
                };

                self.control_depth += 1;
                self.walk_block(accept, branch_ctx);
                self.walk_block(reject, branch_ctx);
                self.control_depth -= 1;
            }

            Statement::Switch { cases, .. } => {
                self.features.branches = self
                    .features
                    .branches
                    .saturating_add(ctx.repeat.min(u32::MAX as u64) as u32);
                self.control_depth += 1;
                for case in cases {
                    self.walk_block(&case.body, ctx);
                }
                self.control_depth -= 1;
            }

            Statement::Loop {
                body,
                continuing,
                break_if,
            } => {
                let bound = self.prove_loop_bound(body, continuing, *break_if);
                let factor = match &bound {
                    LoopBound::Proven(n) => *n,
                    // 证不出界的循环按 1 倍计数, 反正它会被 validator 直接否决。
                    LoopBound::Unproven(_) => 1,
                };
                self.loop_bounds.push(bound);

                self.control_depth += 1;
                let inner = ctx.with_repeat(factor);
                self.walk_block(body, inner);
                self.walk_block(continuing, inner);
                self.control_depth -= 1;
            }

            Statement::ImageStore { .. } => {
                self.features.image_stores = self
                    .features
                    .image_stores
                    .saturating_add(ctx.repeat.min(u32::MAX as u64) as u32);
                if !ctx.guarded {
                    self.unguarded_stores = self.unguarded_stores.saturating_add(1);
                }
            }

            Statement::ControlBarrier(_) | Statement::MemoryBarrier(_) => {
                // Vulkan 要求 barrier 处于工作组一致的控制流中;
                // 放在 if/loop 里在真机上是未定义行为, 常见表现就是整组挂死。
                if self.control_depth > 0 {
                    self.divergent_barriers = self.divergent_barriers.saturating_add(1);
                }
            }

            // 开销不在这里数: naga 要求任何被物化的表达式先经 `Emit` 声明,
            // 所以 Emit 区间已经完整覆盖了所有指令。这里再数一次 value
            // 会把 `vec4 s = imageLoad(...)` 这类写法的访存算成两次。
            Statement::Store { pointer, value } => {
                self.apply_store(*pointer, *value);
            }

            // 调用点: 带着当前的迭代倍率与守卫状态钻进去。
            // Pass A 不跟调用走 (它逐函数各查一遍, 跟进去只会重复计数)。
            Statement::Call { function, .. } if self.counting => {
                self.walk_call(*function, ctx);
            }

            _ => {}
        }
    }

    /// 数一条表达式的开销。只数「这条表达式自身」的开销, 子表达式由 Emit 区间各自计数。
    fn count_expr(&mut self, handle: Handle<Expression>, repeat: u64) {
        let r = repeat.min(u32::MAX as u64) as u32;
        let Some(expr) = self.func.expressions.try_get(handle).ok() else {
            return;
        };
        match expr {
            Expression::ImageLoad { image, .. } => {
                self.features.image_loads = self.features.image_loads.saturating_add(r);
                if let Ok(Expression::GlobalVariable(g)) = self.func.expressions.try_get(*image) {
                    let idx = g.index() as u32;
                    if !self.features.images_read.contains(&idx) {
                        self.features.images_read.push(idx);
                    }
                }
            }
            Expression::ImageSample { .. } => {
                self.features.image_loads = self.features.image_loads.saturating_add(r);
            }
            Expression::ImageQuery { .. } => {
                self.features.image_queries = self.features.image_queries.saturating_add(r);
            }
            Expression::Binary { .. } | Expression::Unary { .. } | Expression::As { .. } => {
                self.features.alu_ops = self.features.alu_ops.saturating_add(r);
            }
            Expression::Select { .. } | Expression::Relational { .. } => {
                self.features.alu_ops = self.features.alu_ops.saturating_add(r);
            }
            Expression::Math { fun, .. } => {
                if is_transcendental(*fun) {
                    self.features.transcendental_ops =
                        self.features.transcendental_ops.saturating_add(r);
                } else {
                    self.features.alu_ops = self.features.alu_ops.saturating_add(r);
                }
            }
            _ => {}
        }
    }

    /// 这个条件是不是从 `imageSize()` 推出来的?
    ///
    /// 沿表达式树往下找, 并且在遇到 `Load(局部变量)` 时顺着该变量的**所有定值**
    /// 继续往回走 —— 短路求值把守卫拆成临时 bool 之后, 只有这样才追得回源头。
    fn expr_reaches_image_size(&self, root: Handle<Expression>) -> bool {
        let mut seen = vec![false; self.func.expressions.len()];
        self.reaches_image_size(root, &mut seen)
    }

    fn reaches_image_size(&self, handle: Handle<Expression>, seen: &mut Vec<bool>) -> bool {
        let idx = handle.index();
        if idx >= seen.len() || seen[idx] {
            return false;
        }
        seen[idx] = true;
        let Ok(expr) = self.func.expressions.try_get(handle) else {
            return false;
        };
        if matches!(
            expr,
            Expression::ImageQuery {
                query: naga::ImageQuery::Size { .. },
                ..
            }
        ) {
            return true;
        }

        // 读局部变量 → 追它的定值链。
        if let Expression::Load { pointer } = expr {
            if let Some(v) = base_local(self.func, *pointer) {
                if let Some(defs) = self.local_defs.get(v.index()) {
                    for def in defs.clone() {
                        if self.reaches_image_size(def, seen) {
                            return true;
                        }
                    }
                }
            }
        }

        let mut hit = false;
        walk_operands(expr, &mut |h| {
            if !hit {
                hit = self.reaches_image_size(h, seen);
            }
        });
        hit
    }

    /// 函数入口处的常量环境: 取各局部变量的声明初值。
    fn seed_const_env(&self) -> Vec<Option<i64>> {
        self.func
            .local_variables
            .iter()
            .map(|(_, lv)| lv.init.and_then(|h| self.const_i64(h)))
            .collect()
    }

    /// 顺序执行到某条 Store 时更新常量环境。
    fn apply_store(&mut self, pointer: Handle<Expression>, value: Handle<Expression>) {
        let Some(v) = base_local(self.func, pointer) else {
            return;
        };
        let folded = self.const_i64(value);
        if let Some(slot) = self.const_env.get_mut(v.index()) {
            *slot = folded;
        }
    }

    // ---------- 归纳变量分析 ----------

    /// 试图为一个循环证明有限迭代上界。
    ///
    /// 只认这一种可证形式 (naga 从 `for` / `while` 降下来的规范形态):
    ///   1. 循环体顶层有一条 `if <cond> { break; }`, 或者 `break_if = <cond>`;
    ///   2. 该 cond (剥掉 LogicalNot 后) 形如 `<局部变量 v> <比较符> <常量>`;
    ///   3. `continuing` 块 (或循环体) 里有唯一一条 `v = v ± <非零常量>`;
    ///   4. v 的初值是常量。
    ///
    /// 四件事齐了才算数, 缺一律 Unproven。
    fn prove_loop_bound(
        &self,
        body: &Block,
        continuing: &Block,
        break_if: Option<Handle<Expression>>,
    ) -> LoopBound {
        let Some(exit) = self.find_exit_condition(body, break_if) else {
            return LoopBound::Unproven(
                "循环体里找不到任何 break 出口 (无界循环: while(true) / for(;;) / do..while(true))"
                    .to_string(),
            );
        };

        let Some(cmp) = self.parse_comparison(exit) else {
            return LoopBound::Unproven(
                "循环出口条件不是「局部变量 与 编译期常量 比较」的形式, 迭代次数无法静态定界"
                    .to_string(),
            );
        };

        let Some(step) = self
            .find_step(continuing, cmp.var)
            .or_else(|| self.find_step(body, cmp.var))
        else {
            return LoopBound::Unproven(format!(
                "找不到归纳变量的常量步进 (形如 i = i + 1); 变量槽 #{} 在循环内没有可静态求值的更新",
                cmp.var.index()
            ));
        };

        let Some(init) = self.local_init(cmp.var) else {
            return LoopBound::Unproven(format!(
                "归纳变量 (槽 #{}) 的初值不是编译期常量, 迭代次数无法静态定界",
                cmp.var.index()
            ));
        };

        match trip_count(init, cmp.bound, cmp.op, cmp.var_on_left, step) {
            Some(n) => LoopBound::Proven(n),
            None => LoopBound::Unproven(format!(
                "归纳变量单调方向与出口条件矛盾 (init={init}, bound={}, step={step}): 循环永不退出",
                cmp.bound
            )),
        }
    }

    /// 找出「跳出循环」的那个条件表达式。
    fn find_exit_condition(
        &self,
        body: &Block,
        break_if: Option<Handle<Expression>>,
    ) -> Option<ExitCond> {
        if let Some(cond) = break_if {
            return Some(ExitCond {
                expr: cond,
                negated: false,
            });
        }
        // 只看循环体顶层: 嵌套更深的 break 属于「条件跳出」, 不构成迭代上界。
        for stmt in body.iter() {
            if let Statement::If {
                condition,
                accept,
                reject,
            } = stmt
            {
                if block_breaks(accept) {
                    return Some(ExitCond {
                        expr: *condition,
                        negated: false,
                    });
                }
                if block_breaks(reject) {
                    return Some(ExitCond {
                        expr: *condition,
                        negated: true,
                    });
                }
            }
        }
        None
    }

    /// 把出口条件解析成 `v <op> const`。
    fn parse_comparison(&self, exit: ExitCond) -> Option<Comparison> {
        // 剥 LogicalNot: naga 把 `for (…; i < n; …)` 降成 `if !(i < n) { break }`。
        let mut handle = exit.expr;
        let mut negated = exit.negated;
        while let Ok(Expression::Unary {
            op: naga::UnaryOperator::LogicalNot,
            expr,
        }) = self.func.expressions.try_get(handle)
        {
            negated = !negated;
            handle = *expr;
        }

        let Expression::Binary { op, left, right } = self.func.expressions.try_get(handle).ok()?
        else {
            return None;
        };

        // `negated == false` 时该条件成立即 break, 故「继续循环」的条件是它的反面。
        let continue_op = if negated { *op } else { negate_cmp(*op)? };

        let (var, bound, var_on_left) = match (self.as_local_load(*left), self.const_i64(*right)) {
            (Some(v), Some(c)) => (v, c, true),
            _ => match (self.const_i64(*left), self.as_local_load(*right)) {
                (Some(c), Some(v)) => (v, c, false),
                _ => return None,
            },
        };

        Some(Comparison {
            var,
            bound,
            op: continue_op,
            var_on_left,
        })
    }

    /// `Load { pointer: LocalVariable(v) }` → v
    fn as_local_load(&self, handle: Handle<Expression>) -> Option<Handle<naga::LocalVariable>> {
        let Expression::Load { pointer } = self.func.expressions.try_get(handle).ok()? else {
            return None;
        };
        match self.func.expressions.try_get(*pointer).ok()? {
            Expression::LocalVariable(v) => Some(*v),
            _ => None,
        }
    }

    /// 在一个块的顶层找 `v = v ± const`, 返回步长 (带符号)。
    fn find_step(&self, block: &Block, var: Handle<naga::LocalVariable>) -> Option<i64> {
        let mut found: Option<i64> = None;
        for stmt in block.iter() {
            let (pointer, value) = match stmt {
                Statement::Store { pointer, value } => (*pointer, *value),
                Statement::Block(inner) => {
                    if let Some(s) = self.find_step(inner, var) {
                        // 同一个变量在多处被改 → 不可证。
                        if found.is_some() {
                            return None;
                        }
                        found = Some(s);
                    }
                    continue;
                }
                _ => continue,
            };

            let is_target = matches!(
                self.func.expressions.try_get(pointer).ok(),
                Some(Expression::LocalVariable(v)) if *v == var
            );
            if !is_target {
                continue;
            }
            if found.is_some() {
                return None;
            }

            let Some(Expression::Binary { op, left, right }) =
                self.func.expressions.try_get(value).ok()
            else {
                return None;
            };
            let sign = match op {
                naga::BinaryOperator::Add => 1i64,
                naga::BinaryOperator::Subtract => -1i64,
                _ => return None,
            };
            let delta = match (self.as_local_load(*left), self.const_i64(*right)) {
                (Some(v), Some(c)) if v == var => c,
                _ => match (self.const_i64(*left), self.as_local_load(*right)) {
                    // `i = 1 - i` 不是单调步进, 直接不认。
                    (Some(_), Some(v)) if v == var && sign > 0 => return None,
                    _ => return None,
                },
            };
            if delta == 0 {
                return None;
            }
            found = Some(sign * delta);
        }
        found
    }

    /// 进入循环那一刻, 归纳变量的值。
    ///
    /// 不能只看 `LocalVariable.init`: 嵌套循环里的内层计数器每次重入都要复位,
    /// naga 因此把初值放在循环前的一条 `Store` 上而不是声明初值上
    /// (`for (dy…) { for (dx = -1; …) }` 的 `dx` 就是这种)。
    /// 常量环境按语句顺序前向传播, 正好抓的是这个值。
    fn local_init(&self, var: Handle<naga::LocalVariable>) -> Option<i64> {
        self.const_env.get(var.index()).copied().flatten()
    }

    /// 保守的整数常量求值器: 字面量 / 取负 / 模块常量 / 零值。
    fn const_i64(&self, handle: Handle<Expression>) -> Option<i64> {
        match self.func.expressions.try_get(handle).ok()? {
            Expression::Literal(lit) => literal_i64(lit),
            Expression::ZeroValue(_) => Some(0),
            Expression::Unary {
                op: naga::UnaryOperator::Negate,
                expr,
            } => self.const_i64(*expr).map(|v| -v),
            Expression::Constant(c) => {
                let init = self.module.constants.try_get(*c).ok()?.init;
                match self.module.global_expressions.try_get(init).ok()? {
                    Expression::Literal(lit) => literal_i64(lit),
                    Expression::ZeroValue(_) => Some(0),
                    _ => None,
                }
            }
            _ => None,
        }
    }
}

#[derive(Clone, Copy)]
struct ExitCond {
    expr: Handle<Expression>,
    /// true 表示「条件为假时 break」。
    negated: bool,
}

struct Comparison {
    var: Handle<naga::LocalVariable>,
    bound: i64,
    /// 「继续循环」所需满足的比较运算。
    op: naga::BinaryOperator,
    /// 归纳变量是否在比较式左侧。
    var_on_left: bool,
}

fn literal_i64(lit: &Literal) -> Option<i64> {
    match lit {
        Literal::I32(v) => Some(*v as i64),
        Literal::U32(v) => Some(*v as i64),
        Literal::I64(v) => Some(*v),
        Literal::U64(v) => i64::try_from(*v).ok(),
        Literal::I16(v) => Some(*v as i64),
        Literal::U16(v) => Some(*v as i64),
        Literal::AbstractInt(v) => Some(*v),
        _ => None,
    }
}

fn negate_cmp(op: naga::BinaryOperator) -> Option<naga::BinaryOperator> {
    use naga::BinaryOperator as B;
    Some(match op {
        B::Less => B::GreaterEqual,
        B::LessEqual => B::Greater,
        B::Greater => B::LessEqual,
        B::GreaterEqual => B::Less,
        B::Equal => B::NotEqual,
        B::NotEqual => B::Equal,
        _ => return None,
    })
}

/// 由 (初值, 界, 继续条件, 步长) 算最大迭代次数。
///
/// 语义: 变量从 `init` 出发, 只要 `continue_op` 成立就再跑一轮, 每轮 `v += step`。
/// 返回 `None` = 证不出会退出 (死循环), `Some(0)` = 一次都不进。
fn trip_count(
    init: i64,
    bound: i64,
    continue_op: naga::BinaryOperator,
    var_on_left: bool,
    step: i64,
) -> Option<u64> {
    use naga::BinaryOperator as B;
    if step == 0 {
        return None;
    }

    // 统一成「变量在左」的形式。
    let op = if var_on_left {
        continue_op
    } else {
        match continue_op {
            B::Less => B::Greater,
            B::LessEqual => B::GreaterEqual,
            B::Greater => B::Less,
            B::GreaterEqual => B::LessEqual,
            other => other,
        }
    };

    // 向上取整的正整数除法。
    let ceil_div = |a: i64, b: i64| -> i64 { (a + b - 1) / b };

    Some(match op {
        // 继续条件 v < bound: 必须向上走才可能失效。
        B::Less => {
            if init >= bound {
                0
            } else if step < 0 {
                return None; // 起点已满足条件却一路远离上界 → 永不退出
            } else {
                ceil_div(bound - init, step) as u64
            }
        }
        B::LessEqual => {
            if init > bound {
                0
            } else if step < 0 {
                return None;
            } else {
                ((bound - init) / step + 1) as u64
            }
        }
        // 继续条件 v > bound: 必须向下走。
        B::Greater => {
            if init <= bound {
                0
            } else if step > 0 {
                return None;
            } else {
                ceil_div(init - bound, -step) as u64
            }
        }
        B::GreaterEqual => {
            if init < bound {
                0
            } else if step > 0 {
                return None;
            } else {
                ((init - bound) / (-step) + 1) as u64
            }
        }
        // v != bound: 只有步长恰好能落在 bound 上才停得住。
        B::NotEqual => {
            if init == bound {
                0
            } else {
                let span = bound - init;
                if span % step != 0 || span / step < 0 {
                    return None;
                }
                (span / step) as u64
            }
        }
        // v == bound 作为继续条件: 跑完一轮就不再相等 (step != 0)。
        B::Equal => {
            if init == bound {
                1
            } else {
                0
            }
        }
        _ => return None,
    })
}

fn block_breaks(block: &Block) -> bool {
    block.iter().any(|s| matches!(s, Statement::Break))
}

/// 块的每条执行路径是否都以 return 结束。
fn block_always_returns(block: &Block) -> bool {
    for stmt in block.iter() {
        match stmt {
            Statement::Return { .. } | Statement::Kill => return true,
            Statement::Block(inner) => {
                if block_always_returns(inner) {
                    return true;
                }
            }
            Statement::If { accept, reject, .. }
                if block_always_returns(accept) && block_always_returns(reject) =>
            {
                return true;
            }
            _ => {}
        }
    }
    false
}

/// 遍历一条表达式的直接操作数。
fn walk_operands(expr: &Expression, f: &mut impl FnMut(Handle<Expression>)) {
    use Expression as E;
    match expr {
        E::Access { base, index } => {
            f(*base);
            f(*index);
        }
        E::AccessIndex { base, .. } => f(*base),
        E::Splat { value, .. } => f(*value),
        E::Swizzle { vector, .. } => f(*vector),
        E::Compose { components, .. } => components.iter().for_each(|c| f(*c)),
        E::Load { pointer } => f(*pointer),
        E::ImageSample {
            image,
            coordinate,
            array_index,
            offset,
            ..
        } => {
            f(*image);
            f(*coordinate);
            array_index.map(&mut *f);
            offset.map(&mut *f);
        }
        E::ImageLoad {
            image,
            coordinate,
            array_index,
            sample,
            level,
        } => {
            f(*image);
            f(*coordinate);
            array_index.map(&mut *f);
            sample.map(&mut *f);
            level.map(&mut *f);
        }
        E::ImageQuery { image, query } => {
            f(*image);
            if let naga::ImageQuery::Size { level: Some(l) } = query {
                f(*l);
            }
        }
        E::Unary { expr, .. } => f(*expr),
        E::Binary { left, right, .. } => {
            f(*left);
            f(*right);
        }
        E::Select {
            condition,
            accept,
            reject,
        } => {
            f(*condition);
            f(*accept);
            f(*reject);
        }
        E::Derivative { expr, .. } => f(*expr),
        E::Relational { argument, .. } => f(*argument),
        E::Math {
            arg,
            arg1,
            arg2,
            arg3,
            ..
        } => {
            f(*arg);
            arg1.map(&mut *f);
            arg2.map(&mut *f);
            arg3.map(&mut *f);
        }
        E::As { expr, .. } => f(*expr),
        E::ArrayLength(h) => f(*h),
        _ => {}
    }
}

fn is_transcendental(fun: naga::MathFunction) -> bool {
    use naga::MathFunction as M;
    matches!(
        fun,
        M::Sin
            | M::Cos
            | M::Tan
            | M::Asin
            | M::Acos
            | M::Atan
            | M::Atan2
            | M::Sinh
            | M::Cosh
            | M::Tanh
            | M::Asinh
            | M::Acosh
            | M::Atanh
            | M::Pow
            | M::Exp
            | M::Exp2
            | M::Log
            | M::Log2
            | M::Sqrt
            | M::InverseSqrt
            | M::Inverse
            | M::Determinant
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use naga::BinaryOperator as B;

    /// 归纳变量算术是整个防爆门禁的地基, 单独钉死。
    #[test]
    fn trip_count_counts_ordinary_ascending_loops() {
        // for (i = 0; i < 3; i++)
        assert_eq!(trip_count(0, 3, B::Less, true, 1), Some(3));
        // for (i = -1; i <= 2; i++)  —— 4x4 邻域的那种写法
        assert_eq!(trip_count(-1, 2, B::LessEqual, true, 1), Some(4));
        // 步长 2
        assert_eq!(trip_count(0, 10, B::Less, true, 2), Some(5));
        // 步长不整除上界时向上取整
        assert_eq!(trip_count(0, 10, B::Less, true, 3), Some(4));
    }

    #[test]
    fn trip_count_counts_descending_loops() {
        // for (i = 3; i > 0; i--)
        assert_eq!(trip_count(3, 0, B::Greater, true, -1), Some(3));
        // for (i = 3; i >= 0; i--)
        assert_eq!(trip_count(3, 0, B::GreaterEqual, true, -1), Some(4));
    }

    #[test]
    fn trip_count_returns_zero_when_the_loop_never_runs() {
        assert_eq!(trip_count(5, 3, B::Less, true, 1), Some(0));
        assert_eq!(trip_count(0, 3, B::Greater, true, -1), Some(0));
    }

    /// 起点满足条件, 但步进方向背离上界 —— 这是必须被识破的死循环。
    #[test]
    fn trip_count_rejects_counters_moving_away_from_the_bound() {
        assert_eq!(trip_count(0, 8, B::Less, true, -1), None);
        assert_eq!(trip_count(8, 0, B::Greater, true, 1), None);
        assert_eq!(trip_count(0, 8, B::Less, true, 0), None);
    }

    /// `i != N` 步长跨过 N 就永远不相等。
    #[test]
    fn trip_count_handles_inequality_exits() {
        assert_eq!(trip_count(0, 6, B::NotEqual, true, 2), Some(3));
        assert_eq!(trip_count(0, 5, B::NotEqual, true, 2), None);
        assert_eq!(trip_count(0, 5, B::NotEqual, true, -1), None);
    }

    /// 比较式写成 `3 > i` 时语义要跟着翻转。
    #[test]
    fn trip_count_normalises_reversed_comparisons() {
        assert_eq!(trip_count(0, 3, B::Greater, false, 1), Some(3));
        assert_eq!(trip_count(0, 3, B::GreaterEqual, false, 1), Some(4));
    }
}
