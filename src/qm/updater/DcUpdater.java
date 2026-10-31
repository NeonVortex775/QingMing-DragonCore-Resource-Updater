package qm.updater;

import net.minecraftforge.fml.relauncher.IFMLLoadingPlugin;

import java.awt.GraphicsEnvironment;
import java.io.*;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 青冥 · 资源自动更新器（Forge 1.12.2 CoreMod）
 *
 * 在游戏资源加载之前，比对远端 manifest 与本地 .dcsync.json，
 * 只下载有变化的 / 缺失的文件，写入 resourcepacks/DragonCore/ 下 manifest 列出的路径。
 *
 * 设计约束：
 *  - 绝不引用任何 Minecraft / Forge 游戏类（只在 deobf 之前跑）
 *  - 任何异常都吞掉，绝不影响启动
 *  - 日常启动预算 20s（无更新时 < 1s）；首次安装预算 15 分钟
 *  - 多线程下载（默认 6），否则 7000+ 个小文件光往返就要好几分钟
 *  - 大批量下载时弹一个进度条，避免玩家以为卡死
 *  - 只写 manifest 里列出的文件，不删除任何本地文件
 *
 * 可用 JVM 参数（PCL：版本设置 → 高级 → JVM 参数）：
 *  -Ddcmc.base=http://127.0.0.1:8080/dc/   覆盖资源地址
 *  -Ddcmc.threads=6                        下载线程数
 *  -Ddcmc.budget=20                        日常预算（秒）
 *  -Ddcmc.budget.first=900                 首次安装预算（秒）
 *  -Ddcmc.gui=false                        关闭进度条
 */
@IFMLLoadingPlugin.MCVersion("1.12.2")
@IFMLLoadingPlugin.TransformerExclusions({"qm.updater."})
public class DcUpdater implements IFMLLoadingPlugin {

    /**
     * 资源地址的三个来源，优先级从高到低：
     *   1. JVM 参数 -Ddcmc.base=http://x.x.x.x:port/dc/
     *   2. 客户端配置文件 .minecraft/config/qingming-updater.txt（第一行非注释内容）
     *      或 .minecraft/qingming-updater.txt
     *   3. 下面这个编译期默认值（留空 = 必须带配置文件，没有配置就静默跳过）
     * 所以换服务器不需要重新打包 jar，发给玩家时附带一个 txt 即可。
     */
    private static final String BASE = "";
    /** 客户端配置文件里的地址（可能为 null） */
    private static final String[] BASE_FILES = {
            "config/qingming-updater.txt",
            "qingming-updater.txt",
    };
    private static final String PACK_DIR = "resourcepacks/DragonCore";
    private static final String STATE_NAME = ".dcsync.json";
    private static final String LOG_NAME = ".dcsync.log";

    private static final int CONNECT_TIMEOUT_MS = 4000;
    private static final int READ_TIMEOUT_MS = 12000;
    private static final long BUDGET_MS = 20000L;            // 日常
    private static final long BUDGET_FIRST_MS = 900000L;     // 首次安装 15 分钟
    /** 待下载文件数超过这个值才弹进度条，日常小更新不打扰 */
    private static final int GUI_MIN_FILES = 60;

    private static final Pattern FILE_ENTRY = Pattern.compile(
            "\"((?:[^\"\\\\]|\\\\.)*)\"\\s*:\\s*\\{\\s*\"sha1\"\\s*:\\s*\"([0-9a-fA-F]{40})\"\\s*,"
                    + "\\s*\"size\"\\s*:\\s*(\\d+)\\s*\\}");

    private static volatile boolean ran = false;
    /** injectData 拿到的 .minecraft 目录，供 base() 找配置文件 */
    private static volatile File gameDirRef = null;

    // ---------- IFMLLoadingPlugin ----------

    @Override
    public String[] getASMTransformerClass() {
        return new String[0];
    }

    @Override
    public String getModContainerClass() {
        return null;
    }

    @Override
    public String getSetupClass() {
        return null;
    }

    @Override
    public String getAccessTransformerClass() {
        return null;
    }

    @Override
    public void injectData(Map<String, Object> data) {
        if (ran) {
            return;
        }
        ran = true;
        long t0 = System.currentTimeMillis();

        File gameDir = null;
        try {
            Object o = data.get("mcLocation");
            if (o instanceof File) {
                gameDir = (File) o;
            }
        } catch (Throwable ignored) {
        }
        if (gameDir == null) {
            return;
        }
        gameDirRef = gameDir;
        File pack = new File(gameDir, PACK_DIR);
        try {
            run(pack, t0);
        } catch (Throwable t) {
            note(pack, "更新失败（不影响启动）：" + t,
                    "update failed (ignored): " + t.getClass().getSimpleName());
        }
    }

    // ---------- 配置读取 ----------

    /** 地址来源：-Ddcmc.base > 客户端 txt 配置 > 编译期默认（可为空） */
    private static String base() {
        String v = System.getProperty("dcmc.base");
        if (v == null || v.trim().isEmpty()) {
            v = baseFromFile();
        }
        if (v == null || v.trim().isEmpty()) {
            v = BASE;
        }
        v = v == null ? "" : v.trim();
        if (v.isEmpty()) {
            return "";
        }
        return v.endsWith("/") ? v : v + "/";
    }

    /** 读 .minecraft/config/qingming-updater.txt 的第一行有效内容 */
    private static String baseFromFile() {
        File g = gameDirRef;
        if (g == null) {
            return null;
        }
        for (String rel : BASE_FILES) {
            File f = new File(g, rel);
            if (!f.isFile()) {
                continue;
            }
            BufferedReader r = null;
            try {
                r = new BufferedReader(new InputStreamReader(new FileInputStream(f), "UTF-8"));
                String line;
                while ((line = r.readLine()) != null) {
                    line = line.trim();
                    if (line.length() == 0 || line.startsWith("#")) {
                        continue;
                    }
                    return line;
                }
            } catch (Throwable ignored) {
                // 读不到就用默认地址，绝不影响启动
            } finally {
                if (r != null) {
                    try {
                        r.close();
                    } catch (Throwable ignored) {
                    }
                }
            }
        }
        return null;
    }

    private static int intProp(String key, int def) {
        try {
            String v = System.getProperty(key);
            if (v == null || v.trim().isEmpty()) {
                return def;
            }
            int n = Integer.parseInt(v.trim());
            return n <= 0 ? def : n;
        } catch (Throwable t) {
            return def;
        }
    }

    private static boolean boolProp(String key, boolean def) {
        try {
            String v = System.getProperty(key);
            if (v == null || v.trim().isEmpty()) {
                return def;
            }
            return !"false".equalsIgnoreCase(v.trim()) && !"0".equals(v.trim());
        } catch (Throwable t) {
            return def;
        }
    }

    // ---------- 主流程 ----------

    private void run(File pack, long t0) throws Exception {
        if (!pack.isDirectory() && !pack.mkdirs()) {
            return;
        }
        if (base().isEmpty()) {
            note(pack, "资源地址未配置（缺 qingming-updater.txt 或 -Ddcmc.base），跳过更新");
            return;
        }
        File stateFile = new File(pack, STATE_NAME);
        boolean firstRun = !stateFile.isFile();

        int budgetSec = firstRun
                ? intProp("dcmc.budget.first", (int) (BUDGET_FIRST_MS / 1000))
                : intProp("dcmc.budget", (int) (BUDGET_MS / 1000));
        final long budget = budgetSec * 1000L;

        String remoteVer = httpText(base() + "version.txt", t0);
        if (remoteVer == null) {
            return;                       // 服务器不可达：静默跳过，用本地资源
        }
        remoteVer = remoteVer.trim();

        Map<String, String> state = readState(stateFile);
        if (remoteVer.equals(state.get("__version")) && localIntact(pack, state)
                && stateComplete(state)) {
            return;                       // 已是最新且文件齐全
        }

        String manifest = httpText(base() + "manifest.json", t0);
        if (manifest == null) {
            return;
        }

        Map<String, String[]> entries = parse(manifest);   // path -> [sha1, size]
        if (entries.isEmpty()) {
            note(pack, "manifest 解析为空，放弃");
            return;
        }

        int skip = 0;
        final List<String[]> todo = new ArrayList<String[]>();   // {rel, sha1, size, known}
        Map<String, String> next = Collections.synchronizedMap(new LinkedHashMap<String, String>());

        for (Map.Entry<String, String[]> e : entries.entrySet()) {
            String rel = e.getKey();
            String sha = e.getValue()[0];
            long size = Long.parseLong(e.getValue()[1]);

            File local = new File(pack, rel);
            String knownVal = state.get(rel);
            String knownSha = null;
            if (knownVal != null) {
                int bar = knownVal.indexOf('|');
                knownSha = bar < 0 ? knownVal : knownVal.substring(0, bar);
            }

            boolean upToDate = sha.equals(knownSha) && local.isFile() && local.length() == size;
            // 首次运行（没有本地记录）：文件存在且大小一致就信任，避免新客户端全量重下
            if (!upToDate && knownVal == null && local.isFile() && local.length() == size) {
                upToDate = true;
            }
            if (upToDate) {
                next.put(rel, sha + "|" + size);
                skip++;
                continue;
            }
            todo.add(new String[]{rel, sha, String.valueOf(size)});
        }

        if (todo.isEmpty()) {
            finish(pack, stateFile, next, remoteVer, 0, 0, skip, 0, false, entries.size());
            return;
        }

        long totalBytes = 0;
        for (String[] t : todo) {
            totalBytes += Long.parseLong(t[2]);
        }

        final int threads = Math.min(intProp("dcmc.threads", 6), 16);
        final AtomicInteger doneIdx = new AtomicInteger();
        final AtomicInteger down = new AtomicInteger();
        final AtomicInteger fail = new AtomicInteger();
        final AtomicLong bytes = new AtomicLong();
        final AtomicBoolean abort = new AtomicBoolean(false);
        final int total = todo.size();
        final long totalB = totalBytes;

        System.out.println("[QingMing] downloading " + total + " files ("
                + (totalBytes / 1048576) + " MB) with " + threads + " threads ...");

        Progress pg = Progress.open(pack, total, totalBytes);

        ExecutorService pool = Executors.newFixedThreadPool(threads);
        try {
            for (final String[] t : todo) {
                pool.submit(new Runnable() {
                    @Override
                    public void run() {
                        try {
                            if (abort.get()) {
                                return;
                            }
                            if (System.currentTimeMillis() - t0 > budget) {
                                abort.set(true);
                                return;
                            }
                            String rel = t[0];
                            long size = Long.parseLong(t[2]);
                            // 免费 frp 节点并发高了会偶发超时（实测 0.85%），重试 3 次足够
                            byte[] data = null;
                            for (int attempt = 0; attempt < 3 && data == null; attempt++) {
                                if (attempt > 0) {
                                    try {
                                        Thread.sleep(120L * attempt);
                                    } catch (InterruptedException ie) {
                                        Thread.currentThread().interrupt();
                                    }
                                }
                                data = httpBytes(base() + encodePath(rel), t0);
                            }
                            boolean ok = false;
                            if (data != null) {
                                ok = writeAtomic(new File(pack, rel), data);
                            }
                            int d = doneIdx.incrementAndGet();
                            if (ok) {
                                down.incrementAndGet();
                                bytes.addAndGet(data.length);
                                next.put(rel, t[1] + "|" + size);
                            } else {
                                fail.incrementAndGet();
                            }
                            if (pg != null) {
                                pg.update(d, total, bytes.get(), totalB);
                            }
                        } catch (Throwable ignored) {
                        }
                    }
                });
            }
        } finally {
            pool.shutdown();
            try {
                pool.awaitTermination(Math.max(30, budget / 1000 + 60), TimeUnit.SECONDS);
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
            }
            if (pg != null) {
                pg.close();
            }
        }

        boolean truncated = abort.get() || (doneIdx.get() + skip < entries.size());
        finish(pack, stateFile, next, remoteVer,
                down.get(), bytes.get(), skip, fail.get(), truncated, entries.size());
    }

    /** 写状态 + 记账。truncated 时不写版本号，下次启动继续（已下的会被 sha 命中而跳过）。 */
    private void finish(File pack, File stateFile,
                        Map<String, String> next, String remoteVer,
                        int down, long bytes, int skip, int fail, boolean truncated,
                        int totalEntries) {
        Map<String, String> out = new LinkedHashMap<String, String>();
        out.put("__version", truncated ? "" : remoteVer);
        out.put("__count", String.valueOf(totalEntries));
        out.putAll(next);
        writeState(stateFile, out);

        if (truncated) {
            note(pack, String.format("更新未完成：已下 %d / 共 %d，下次启动继续", down, down + skip + fail),
                    String.format("update incomplete: %d done, %d failed, resume next launch",
                            down, fail));
        } else if (down > 0) {
            note(pack, String.format("资源已更新到 %s：下载 %d 个（%.1f MB），跳过 %d 个，失败 %d 个",
                    remoteVer, down, bytes / 1048576.0, skip, fail),
                    String.format("resources updated to %s: %d downloaded (%d KB), %d skipped, %d failed",
                            remoteVer, down, bytes / 1024, skip, fail));
        } else if (fail > 0) {
            note(pack, String.format("建立资源索引 %s：%d 个文件全部失败", remoteVer, fail),
                    String.format("index failed for %s: %d files failed", remoteVer, fail));
        } else {
            note(pack, String.format("建立资源索引 %s：本地 %d 个文件已全部匹配，无需下载",
                    remoteVer, skip),
                    String.format("index built for %s: %d files already match", remoteVer, skip));
        }
    }

    /** 版本号一致时，用上次的记录做一次本地自检（只 stat，不读内容），防止文件被删/截断 */
    private boolean localIntact(File pack, Map<String, String> state) {
        for (Map.Entry<String, String> e : state.entrySet()) {
            String k = e.getKey();
            if (k.startsWith("__")) {
                continue;
            }
            String v = e.getValue();
            int bar = v.indexOf('|');
            if (bar < 0) {
                return false;             // 旧格式没有 size，走完整比对
            }
            long size;
            try {
                size = Long.parseLong(v.substring(bar + 1));
            } catch (NumberFormatException ex) {
                return false;
            }
            File f = new File(pack, e.getKey());
            if (!f.isFile() || f.length() != size) {
                return false;
            }
        }
        return true;
    }

    // ---------- 进度条（可选、失败就静默） ----------

    private static final class Progress {
        private javax.swing.JFrame frame;
        private javax.swing.JProgressBar bar;
        private javax.swing.JLabel label;

        static Progress open(File pack, final int total, final long totalBytes) {
            if (total < GUI_MIN_FILES || !boolProp("dcmc.gui", true)) {
                return null;
            }
            try {
                if (GraphicsEnvironment.isHeadless()) {
                    return null;
                }
                final Progress p = new Progress();
                javax.swing.SwingUtilities.invokeAndWait(new Runnable() {
                    @Override
                    public void run() {
                        try {
                            p.frame = new javax.swing.JFrame("QingMing - loading resources");
                            p.label = new javax.swing.JLabel("0 / " + total + "   ("
                                    + (totalBytes / 1048576) + " MB)", javax.swing.SwingConstants.CENTER);
                            p.label.setBorder(javax.swing.BorderFactory.createEmptyBorder(10, 14, 6, 14));
                            p.bar = new javax.swing.JProgressBar(0, total);
                            p.bar.setBorder(javax.swing.BorderFactory.createEmptyBorder(0, 14, 12, 14));
                            p.bar.setStringPainted(true);
                            p.frame.setLayout(new java.awt.BorderLayout());
                            p.frame.add(p.label, java.awt.BorderLayout.NORTH);
                            p.frame.add(p.bar, java.awt.BorderLayout.CENTER);
                            p.frame.setDefaultCloseOperation(javax.swing.JFrame.DO_NOTHING_ON_CLOSE);
                            p.frame.setSize(380, 96);
                            p.frame.setLocationRelativeTo(null);
                            p.frame.setAlwaysOnTop(true);
                            p.frame.setVisible(true);
                        } catch (Throwable ignored) {
                        }
                    }
                });
                return p.frame == null ? null : p;
            } catch (Throwable t) {
                return null;
            }
        }

        void update(final int done, final int total, final long bytes, final long totalBytes) {
            if (frame == null) {
                return;
            }
            javax.swing.SwingUtilities.invokeLater(new Runnable() {
                @Override
                public void run() {
                    try {
                        bar.setValue(done);
                        label.setText(done + " / " + total + "   ("
                                + (bytes / 1048576) + " / " + (totalBytes / 1048576) + " MB)");
                    } catch (Throwable ignored) {
                    }
                }
            });
        }

        void close() {
            if (frame == null) {
                return;
            }
            try {
                javax.swing.SwingUtilities.invokeLater(new Runnable() {
                    @Override
                    public void run() {
                        try {
                            frame.setVisible(false);
                            frame.dispose();
                        } catch (Throwable ignored) {
                        }
                    }
                });
            } catch (Throwable ignored) {
            }
        }
    }

    /**
     * 索引条数是否等于上次那份 manifest 的文件总数。
     * 少了就说明上次有文件没下成（失败的不进索引，单靠 stat 自检看不出来），必须重新比对一遍。
     */
    private boolean stateComplete(Map<String, String> state) {
        String c = state.get("__count");
        if (c == null) {
            return false;                 // 老格式没有计数，走一次完整比对
        }
        int expect;
        try {
            expect = Integer.parseInt(c);
        } catch (NumberFormatException e) {
            return false;
        }
        int actual = 0;
        for (String k : state.keySet()) {
            if (!k.startsWith("__")) {
                actual++;
            }
        }
        return actual >= expect;
    }

    // ---------- state ----------

    private Map<String, String> readState(File f) {
        Map<String, String> m = new LinkedHashMap<String, String>();
        if (!f.isFile()) {
            return m;
        }
        try {
            String s = new String(readAll(f), StandardCharsets.UTF_8);
            Matcher mt = Pattern.compile("\"((?:[^\"\\\\]|\\\\.)*)\"\\s*:\\s*\"([^\"]*)\"").matcher(s);
            while (mt.find()) {
                String k = unescape(mt.group(1));
                // 落盘时版本号字段叫 version，内存里统一用 __version，避免和文件名撞
                m.put("version".equals(k) ? "__version" : k, mt.group(2));
            }
        } catch (Throwable ignored) {
        }
        return m;
    }

    private void writeState(File f, Map<String, String> m) {
        StringBuilder sb = new StringBuilder("{\"version\":\"");
        sb.append(m.get("__version") == null ? "" : m.get("__version"));
        sb.append("\",\"__count\":\"").append(m.get("__count") == null ? "0" : m.get("__count"));
        sb.append("\",\"files\":{");
        boolean first = true;
        for (Map.Entry<String, String> e : m.entrySet()) {
            if (e.getKey().startsWith("__")) {
                continue;
            }
            if (!first) {
                sb.append(',');
            }
            first = false;
            sb.append('"').append(escape(e.getKey())).append("\":\"").append(e.getValue()).append('"');
        }
        sb.append("}}");
        writeAtomic(f, sb.toString().getBytes(StandardCharsets.UTF_8));
    }

    private Map<String, String[]> parse(String json) {
        Map<String, String[]> m = new LinkedHashMap<String, String[]>();
        Matcher mt = FILE_ENTRY.matcher(json);
        while (mt.find()) {
            m.put(unescape(mt.group(1)), new String[]{mt.group(2).toLowerCase(), mt.group(3)});
        }
        return m;
    }

    // ---------- io ----------

    private static final int MAX_BYTES = 96 * 1024 * 1024;

    private String httpText(String url, long t0) {
        byte[] b = httpBytes(url, t0);
        return b == null ? null : new String(b, StandardCharsets.UTF_8);
    }

    private byte[] httpBytes(String url, long t0) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(url).openConnection();
            c.setConnectTimeout(CONNECT_TIMEOUT_MS);
            c.setReadTimeout(READ_TIMEOUT_MS);
            c.setRequestProperty("User-Agent", "QingMingUpdater/1.1");
            c.setInstanceFollowRedirects(true);
            int code = c.getResponseCode();
            if (code != 200) {
                return null;
            }
            InputStream in = c.getInputStream();
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            byte[] buf = new byte[32768];
            int n;
            while ((n = in.read(buf)) > 0) {
                bos.write(buf, 0, n);
                if (bos.size() > MAX_BYTES) {
                    break;
                }
            }
            in.close();
            return bos.toByteArray();
        } catch (Throwable t) {
            return null;
        } finally {
            if (c != null) {
                c.disconnect();
            }
        }
    }

    private boolean writeAtomic(File target, byte[] data) {
        try {
            File dir = target.getParentFile();
            if (dir != null && !dir.isDirectory() && !dir.mkdirs()) {
                return false;
            }
            File tmp = new File(target.getPath() + ".dctmp");
            FileOutputStream fos = new FileOutputStream(tmp);
            try {
                fos.write(data);
                fos.getFD().sync();
            } finally {
                fos.close();
            }
            if (target.exists() && !target.delete()) {
                // Windows 下被占用会失败：直接覆盖写
                FileOutputStream direct = new FileOutputStream(target);
                try {
                    direct.write(data);
                } finally {
                    direct.close();
                }
                tmp.delete();
                return true;
            }
            return tmp.renameTo(target);
        } catch (Throwable t) {
            return false;
        }
    }

    private static byte[] readAll(File f) throws IOException {
        FileInputStream in = new FileInputStream(f);
        try {
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) {
                bos.write(buf, 0, n);
            }
            return bos.toByteArray();
        } finally {
            in.close();
        }
    }

    private static String encodePath(String rel) {
        StringBuilder sb = new StringBuilder();
        for (String seg : rel.split("/")) {
            if (seg.isEmpty()) {
                continue;
            }
            if (sb.length() > 0) {
                sb.append('/');
            }
            try {
                sb.append(URLEncoder.encode(seg, "UTF-8").replace("+", "%20"));
            } catch (UnsupportedEncodingException e) {
                sb.append(seg);
            }
        }
        return sb.toString();
    }

    private static String escape(String s) {
        return s.replace("\\", "\\\\").replace("\"", "\\\"");
    }

    private static String unescape(String s) {
        return s.replace("\\\"", "\"").replace("\\\\", "\\");
    }

    private void note(File pack, String msg) {
        note(pack, msg, null);
    }

    /** 控制台只打 ASCII 摘要（Windows 终端是 GBK，中文会乱），中文详情写日志文件 */
    private void note(File pack, String msg, String consoleAscii) {
        try {
            if (consoleAscii != null) {
                System.out.println("[QingMing] " + consoleAscii);
            }
        } catch (Throwable ignored) {
        }
        try {
            File f = new File(pack, LOG_NAME);
            Writer w = new OutputStreamWriter(new FileOutputStream(f, false),
                    StandardCharsets.UTF_8);
            try {
                w.write(new java.text.SimpleDateFormat("yyyy-MM-dd HH:mm:ss")
                        .format(new java.util.Date()) + "  " + msg + "\r\n");
            } finally {
                w.close();
            }
        } catch (Throwable ignored) {
        }
    }
}
