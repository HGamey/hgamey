// phonefarm 剪贴板助手: 以 shell 身份经 app_process 运行, 读/写系统剪贴板, 给 adb 不能输入的 Unicode 文字用。
//   CLASSPATH=/data/local/tmp/pfclip.dex app_process / PfClip get        → 打印 "text:<base64>" / "none" / "error:<原因>"
//   CLASSPATH=/data/local/tmp/pfclip.dex app_process / PfClip set <b64>  → 打印 "ok" / "error:<原因>"
// 系统接口(IClipboard)各版本参数不同, 这里按方法名反射, 依参数类型填: 第一个 String 为调用包名, 其余 String 为 null,
// int 一律 0(用户 0、默认设备 0)。
import android.content.ClipData;
import android.os.IBinder;
import android.util.Base64;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.Comparator;

public final class PfClip {
    private static final String PKG = "com.android.shell";

    public static void main(String[] args) {
        try {
            Object cb = service();
            if (args.length >= 1 && args[0].equals("get")) {
                Object clip = call(cb, "getPrimaryClip", null);
                if (clip instanceof ClipData && ((ClipData) clip).getItemCount() > 0) {
                    CharSequence t = ((ClipData) clip).getItemAt(0).getText();
                    if (t != null) {
                        System.out.println("text:" + Base64.encodeToString(t.toString().getBytes(StandardCharsets.UTF_8), Base64.NO_WRAP));
                        return;
                    }
                }
                System.out.println("none");
            } else if (args.length >= 2 && args[0].equals("set")) {
                String text = new String(Base64.decode(args[1], Base64.DEFAULT), StandardCharsets.UTF_8);
                call(cb, "setPrimaryClip", ClipData.newPlainText("phonefarm", text));
                System.out.println("ok");
            } else {
                System.out.println("error:usage");
            }
        } catch (Throwable e) {
            Throwable c = e.getCause() != null ? e.getCause() : e;
            System.out.println("error:" + c.getClass().getSimpleName() + ":" + c.getMessage());
        }
    }

    private static Object service() throws Exception {
        Class<?> sm = Class.forName("android.os.ServiceManager");
        IBinder b = (IBinder) sm.getMethod("getService", String.class).invoke(null, "clipboard");
        Class<?> stub = Class.forName("android.content.IClipboard$Stub");
        return stub.getMethod("asInterface", IBinder.class).invoke(null, b);
    }

    private static Object call(Object cb, String name, ClipData clip) throws Exception {
        Method[] ms = Arrays.stream(cb.getClass().getMethods()).filter(m -> m.getName().equals(name))
                .sorted(Comparator.comparingInt(Method::getParameterCount)).toArray(Method[]::new);
        Exception last = new NoSuchMethodException(name);
        for (Method m : ms) {
            Class<?>[] ps = m.getParameterTypes();
            Object[] vals = new Object[ps.length];
            boolean pkgSet = false;
            for (int i = 0; i < ps.length; i++) {
                if (ps[i] == ClipData.class) vals[i] = clip;
                else if (ps[i] == String.class) { vals[i] = pkgSet ? null : PKG; pkgSet = true; }
                else if (ps[i] == int.class) vals[i] = 0;
                else if (ps[i] == boolean.class) vals[i] = false;
                else vals[i] = null;
            }
            try {
                return m.invoke(cb, vals);
            } catch (Exception e) {
                last = e;
            }
        }
        throw last;
    }
}
