import java.io.File;
import java.util.HashMap;
import java.util.Map;

/** 本地联调用：直接喂一个 gameDir 给 DcUpdater，不需要启动 Minecraft。 */
public class TestRun {
    public static void main(String[] a) throws Exception {
        File gameDir = new File(a[0]);
        Map<String, Object> data = new HashMap<String, Object>();
        data.put("mcLocation", gameDir);
        new qm.updater.DcUpdater().injectData(data);
    }
}
