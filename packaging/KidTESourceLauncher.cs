using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Forms;

[assembly: AssemblyTitle("Kid TE")]
[assembly: AssemblyProduct("Kid TE")]
[assembly: AssemblyCompany("Kid TE")]
[assembly: AssemblyVersion("1.13.0.0")]
[assembly: AssemblyFileVersion("1.13.0.0")]

internal static class KidTESourceLauncher
{
    [DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr window, int command);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr window);

    private static bool ServerReady()
    {
        try
        {
            var request = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:8793/app");
            request.Timeout = 1200;
            using (var response = (HttpWebResponse)request.GetResponse())
                return response.StatusCode == HttpStatusCode.OK;
        }
        catch (WebException) { return false; }
    }

    private static void OpenExisting()
    {
        foreach (var name in new[] { "python", "pythonw", "KidTE" })
        {
            foreach (var process in Process.GetProcessesByName(name))
            {
                try
                {
                    if (process.MainWindowHandle == IntPtr.Zero ||
                        !process.MainWindowTitle.StartsWith("Kid TE", StringComparison.Ordinal))
                        continue;
                    ShowWindow(process.MainWindowHandle, 9); // SW_RESTORE
                    if (SetForegroundWindow(process.MainWindowHandle)) return;
                }
                catch (InvalidOperationException) { /* Window closed during lookup. */ }
                finally { process.Dispose(); }
            }
        }
        Process.Start(new ProcessStartInfo("http://127.0.0.1:8793/app") { UseShellExecute = true });
    }

    private static string Quote(string value)
    {
        var result = new StringBuilder("\"");
        var backslashes = 0;
        foreach (var ch in value)
        {
            if (ch == '\\') { backslashes++; continue; }
            if (ch == '"')
            {
                result.Append('\\', backslashes * 2 + 1);
                result.Append('"');
                backslashes = 0;
                continue;
            }
            result.Append('\\', backslashes);
            backslashes = 0;
            result.Append(ch);
        }
        result.Append('\\', backslashes * 2);
        result.Append('"');
        return result.ToString();
    }

    [STAThread]
    private static int Main(string[] args)
    {
        var root = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        var python = Path.Combine(root, @"backend\.venv\Scripts\python.exe");
        var entry = Path.Combine(root, @"backend\src\app_entry.py");
        var logPath = Path.Combine(root, @"backend\logs\kid-te-launcher.log");
        if (!File.Exists(python) || !File.Exists(entry))
        {
            MessageBox.Show("Không tìm thấy Python hoặc app_entry.py trong thư mục Kid TE:\n" + root,
                "Kid TE — Không thể khởi động", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 2;
        }
        if (ServerReady())
        {
            try { OpenExisting(); return 0; }
            catch (Exception error)
            {
                MessageBox.Show("Kid TE đang chạy nhưng không mở được giao diện: " + error.Message,
                    "Kid TE", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 1;
            }
        }

        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(logPath));
            using (var log = new StreamWriter(new FileStream(logPath, FileMode.Append,
                       FileAccess.Write, FileShare.ReadWrite), new UTF8Encoding(false)))
            {
                log.AutoFlush = true;
                lock (log) log.WriteLine("[launcher] " + DateTimeOffset.Now.ToString("o") + " starting");
                var command = new StringBuilder(Quote(entry));
                foreach (var arg in args) command.Append(' ').Append(Quote(arg));
                var start = new ProcessStartInfo(python, command.ToString());
                start.WorkingDirectory = root;
                start.UseShellExecute = false;
                start.CreateNoWindow = true;
                start.RedirectStandardOutput = true;
                start.RedirectStandardError = true;
                start.StandardOutputEncoding = Encoding.UTF8;
                start.StandardErrorEncoding = Encoding.UTF8;
                start.EnvironmentVariables["PYTHONUTF8"] = "1";
                using (var child = new Process())
                {
                    child.StartInfo = start;
                    child.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e)
                    {
                        if (e.Data != null) lock (log) log.WriteLine(e.Data);
                    };
                    child.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e)
                    {
                        if (e.Data != null) lock (log) log.WriteLine(e.Data);
                    };
                    child.Start();
                    child.BeginOutputReadLine();
                    child.BeginErrorReadLine();
                    child.WaitForExit();
                    lock (log) log.WriteLine("[launcher] exit=" + child.ExitCode);
                    if (child.ExitCode == 2 && ServerReady())
                    {
                        OpenExisting();
                        return 0;
                    }
                    if (child.ExitCode != 0)
                    {
                        MessageBox.Show("Kid TE thoát với mã " + child.ExitCode +
                            ". Xem nhật ký tại:\n" + logPath,
                            "Kid TE — Lỗi khởi động", MessageBoxButtons.OK, MessageBoxIcon.Error);
                    }
                    return child.ExitCode;
                }
            }
        }
        catch (Exception error)
        {
            MessageBox.Show(error.ToString() + "\n\nNhật ký: " + logPath,
                "Kid TE — Lỗi khởi động", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
