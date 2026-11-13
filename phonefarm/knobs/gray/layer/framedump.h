// framedump.h — 逐帧读回送显图 (插帧/超分的训练与评测数据), vk_layer_refknobs.cpp 调用的接口。
//
// 开关: debug.knobs.framedump=1 (CreateInstance 时读一次)。关着时下面所有函数都是空操作,
// 不加任何钩子 —— 不影响 loadop/upop/npuop 等别的档位的 A/B。
// 抓哪一段: 运行中改 debug.knobs.framedump_go=<场景标签> (层每 10 帧看一次), 改成一个
// 新的非空、非 0 的值就开一段, 抓 debug.knobs.framedump_n 次 present (缺省 600)。
// 具体格式、落点见 framedump.cpp 开头。
// 附件拷贝 (可选): debug.knobs.framedump_att=<名字>=<sig前缀>:<附件下标>,... 在指定 render pass 结束时
// 把它的附件 (如不带 UI 的画面、深度) 拷出来, 和送显图同帧号写盘。不设时下面 fd_att_* 全是空操作。
#pragma once
#include <vulkan/vulkan.h>
#include <string>

typedef VkResult(VKAPI_PTR* PFN_rkSetDeviceLoaderData)(VkDevice, void*);

bool fd_init_from_props();   // CreateInstance 时调一次, 返回是否开启
bool fd_on();
void fd_on_device(VkInstance inst, VkPhysicalDevice phys, VkDevice dev, PFN_vkGetInstanceProcAddr gipa,
                  PFN_vkGetDeviceProcAddr gdpa, PFN_rkSetDeviceLoaderData sdld, uint32_t sole_family);
// sole_family: 建设备时只建了一个队列族就填它 (查不到 present 队列的族时兜底), 否则 UINT32_MAX
void fd_on_destroy_device(VkDevice dev);
// 建 swapchain 前: 能加就给 imageUsage 加 TRANSFER_SRC (读回要用)。返回是否加成了。
bool fd_patch_swapchain_ci(VkDevice dev, VkSwapchainCreateInfoKHR* ci);
void fd_on_swapchain(VkDevice dev, VkSwapchainKHR sc, const VkSwapchainCreateInfoKHR* ci, bool usage_ok);
// present 前: 若在抓帧窗口内, 提交一次拷贝并返回改过等待信号量的 present 信息 (写在 scratch 里);
// 否则原样返回 pi。frame 是本层的全局 present 序号。
const VkPresentInfoKHR* fd_on_present(VkQueue q, const VkPresentInfoKHR* pi, VkPresentInfoKHR* scratch,
                                      uint64_t frame);
// 本模块自己的钩子 (vkGetDeviceQueue / vkGetDeviceQueue2 / vkDestroySwapchainKHR), 没开或不是这几个名字返回空。
PFN_vkVoidFunction fd_hook(const char* name);
std::string fd_json();       // 写进 knobs_layer_out.json 的 "framedump" 一节

// ── 附件拷贝 ──
// 一个 render pass 附件的事实 (vk_layer_refknobs.cpp 在 BeginRenderPass 时从它记的表里填)。
struct FdAttInfo {
    VkImage img = VK_NULL_HANDLE;
    uint32_t fmt = 0, usage = 0, w = 0, h = 0;   // 图的格式、usage、mip 0 尺寸
    uint32_t mip = 0, layer = 0;                 // 视图指向的 mip / 层
    uint32_t final_layout = 0, store = 0, stencil_store = 0;
    bool known = false;                          // 视图/图/render pass 信息都查到了
};
bool fd_att_on();                                // framedump=1 且 framedump_att 有可用规则
// 有规则对上这个 pass: sig 前缀相同, 或者 (fmt<格式> 规则) 第 att 个附件是那个格式。
// w/h 是渲染区, fmts[n] 是 render pass 各附件格式; 段进行中顺带记进 summary 的 sigs_seen (诊断用)。
bool fd_att_wants(uint64_t sig, uint32_t w, uint32_t h, uint32_t n, const uint32_t* fmts);
// EndRenderPass 转发之后调用: 对上规则的附件在同一条命令缓冲里录 屏障 -> 拷进主机可见缓冲 -> 屏障回 finalLayout。
void fd_att_record(VkCommandBuffer cb, uint64_t sig, uint32_t n, const FdAttInfo* att);
void fd_att_on_begin_cb(VkCommandBuffer cb);     // 命令缓冲重新开始录制: 收回它录过但没提交的槽
void fd_att_on_submit(VkQueue q, uint32_t n, const VkCommandBuffer* cbs);   // 提交成功之后调用
