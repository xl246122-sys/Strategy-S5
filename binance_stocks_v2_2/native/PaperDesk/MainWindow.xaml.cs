using System.Net.Http;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using System.Windows.Shapes;
using System.Windows.Threading;

namespace PaperDesk;

public partial class MainWindow : Window
{
    private static readonly string[] TabNames = ["S1", "S2", "S3", "S4", "S5", "状态"];
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(3) };
    private readonly HashSet<string> _seen = [];
    private readonly Dictionary<string, Button> _tabs = [];
    private readonly DispatcherTimer _timer = new() { Interval = TimeSpan.FromSeconds(1) };
    private readonly SolidColorBrush _muted = Freeze(Color.FromRgb(0xA7, 0xA3, 0x9A));
    private readonly SolidColorBrush _ink = Freeze(Color.FromRgb(0xEC, 0xEA, 0xE4));
    private readonly SolidColorBrush _gold = Freeze(Color.FromRgb(0xD7, 0xB5, 0x6D));
    private readonly SolidColorBrush _card = Freeze(Color.FromRgb(0x1A, 0x1C, 0x21));
    private readonly SolidColorBrush _dim = Freeze(Color.FromRgb(0x2C, 0x2E, 0x33));
    private string _current = "状态";
    private string _listKey = "";
    private StackPanel? _listHost;
    private readonly Dictionary<string, NameRow> _listRows = [];
    private bool _primed;
    private JsonElement _state;

    public MainWindow()
    {
        InitializeComponent();
        BuildTabs();
        _timer.Tick += async (_, _) => await Refresh();
        Loaded += async (_, _) =>
        {
            await Refresh();
            _timer.Start();
        };
    }

    private void BuildTabs()
    {
        foreach (var name in TabNames)
        {
            var button = new Button
            {
                Content = name,
                Tag = name,
                Background = Brushes.Transparent,
                BorderThickness = new Thickness(0, 0, 0, 2),
                Padding = new Thickness(0, 12, 0, 12),
                Cursor = System.Windows.Input.Cursors.Hand,
            };
            button.Click += (_, _) =>
            {
                _current = name;
                Paint();
            };
            _tabs[name] = button;
            TabBar.Children.Add(button);
        }
    }

    private async Task Refresh()
    {
        try
        {
            var json = await _http.GetStringAsync("http://127.0.0.1:8765/api/state");
            using var document = JsonDocument.Parse(json);
            _state = document.RootElement.Clone();
            NoticeNewAlerts();
            Paint();
        }
        catch (Exception)
        {
            Body.Children.Clear();
            Body.Children.Add(Line("后端未连接", 18, false));
            Body.Children.Add(Line("等待 http://127.0.0.1:8765", 13, true));
        }
    }

    private void NoticeNewAlerts()
    {
        if (!_state.TryGetProperty("alerts", out var alerts))
        {
            return;
        }
        JsonElement? fresh = null;
        var extra = 0;
        foreach (var alert in alerts.EnumerateArray())
        {
            var id = TextOf(alert, "id");
            if (id.Length == 0 || !_seen.Add(id))
            {
                continue;
            }
            if (!_primed)
            {
                continue;
            }
            var kind = TextOf(alert, "kind");
            if (kind is not ("FILLED" or "WORKING" or "SIGNAL"))
            {
                continue;
            }
            if (fresh is null)
            {
                fresh = alert;
            }
            else
            {
                extra += 1;
            }
        }
        _primed = true;
        if (fresh is null)
        {
            return;
        }
        var item = fresh.Value;
        var more = extra > 0 ? $"，另有 {extra} 条" : "";
        if (TextOf(item, "kind") == "SIGNAL")
        {
            NoticeTitle.Text = $"{TextOf(item, "strategy")} 信号达成 100%";
            NoticeBody.Text = $"{TextOf(item, "symbol")} 估价 {TextOf(item, "price")}{more}";
        }
        else
        {
            NoticeTitle.Text = $"{TextOf(item, "strategy")} {TextOf(item, "text")}";
            NoticeBody.Text = $"{TextOf(item, "side")} {TextOf(item, "symbol")} {TextOf(item, "qty")} @ {TextOf(item, "price")}{more}";
        }
        Notice.Visibility = Visibility.Visible;
    }

    private void Paint()
    {
        foreach (var (name, button) in _tabs)
        {
            var selected = name == _current;
            button.Foreground = selected ? _ink : _muted;
            button.BorderBrush = selected ? _gold : Brushes.Transparent;
        }
        Body.Children.Clear();
        if (_current == "状态")
        {
            PaintStatus();
        }
        else
        {
            PaintDesk(_current);
        }
    }

    private void PaintStatus()
    {
        Body.Children.Add(Line("系统后端", 22, false));
        if (!_state.TryGetProperty("server", out var server))
        {
            Body.Children.Add(Line("后端还没有运行状态字段", 14, true));
            return;
        }
        Body.Children.Add(Line(
            $"PID {TextOf(server, "pid")} · 已运行 {TextOf(server, "uptime_seconds")} 秒 · {TextOf(server, "bind")}",
            13,
            true));
        Body.Children.Add(Line($"行情线程 {TextOf(server, "worker")} · 实盘下单关闭", 13, true));
        Body.Children.Add(Line($"币安 {TextOf(_state, "feed")}", 13, true));
        Body.Children.Add(Line($"Alpaca {TextOf(_state, "alpaca")}", 13, true));
        Body.Children.Add(Line($"日历 {TextOf(_state, "calendar")}", 13, true));
        Body.Children.Add(Line("策略运行状态", 18, false));
        if (!_state.TryGetProperty("desks", out var desks))
        {
            return;
        }
        foreach (var desk in desks.EnumerateArray())
        {
            var halted = desk.TryGetProperty("halt", out var halt) && halt.ValueKind == JsonValueKind.True;
            var detail =
                $"{TextOf(desk, "runtime")} · {TextOf(desk, "reason")}" +
                $" · 权益 {TextOf(desk, "equity")}" +
                $" · 持仓 {Count(desk, "positions")}" +
                $" · 未成交 {Count(desk, "working")}" +
                (halted ? " · 停开" : "");
            AddCard($"{TextOf(desk, "strategy")} {TextOf(desk, "title")}", detail);
        }
    }

    private void PaintDesk(string strategy)
    {
        if (!_state.TryGetProperty("desks", out var desks))
        {
            return;
        }
        JsonElement? found = null;
        foreach (var desk in desks.EnumerateArray())
        {
            if (TextOf(desk, "strategy") == strategy)
            {
                found = desk;
                break;
            }
        }
        if (found is null)
        {
            return;
        }
        var item = found.Value;
        Body.Children.Add(Line($"{strategy} {TextOf(item, "title")}", 22, false));
        Body.Children.Add(Line($"{TextOf(item, "runtime")} · {TextOf(item, "reason")}", 13, true));
        var grid = new UniformGrid { Columns = 2, Margin = new Thickness(0, 12, 0, 12) };
        grid.Children.Add(Stat(TextOf(item, "equity"), "权益 USDC"));
        grid.Children.Add(Stat(TextOf(item, "cash"), "现金 USDC"));
        grid.Children.Add(Stat(Count(item, "positions").ToString(), "持仓"));
        var halted = item.TryGetProperty("halt", out var halt) && halt.ValueKind == JsonValueKind.True;
        grid.Children.Add(Stat(halted ? "停开" : "可评估", "新开仓"));
        Body.Children.Add(grid);
        Body.Children.Add(Line("持仓", 16, false));
        AddRows(item, "positions", "空仓", static row =>
            $"{TextOf(row, "symbol")}  {TextOf(row, "qty")} @ {TextOf(row, "avg")}");
        Body.Children.Add(Line("未成交模拟单", 16, false));
        AddRows(item, "working", "无", static row =>
            $"{TextOf(row, "symbol")}  {TextOf(row, "qty")}  限价 {TextOf(row, "limit")}");
        Body.Children.Add(Line("前 11 只行业 ETF，后 20 只达成度最高的个股", 16, false));
        Body.Children.Add(Line("进度是报价离策略形态有多近，到 100% 才弹窗。", 12, true));
        EnsureNames(item);
    }

    private void EnsureNames(JsonElement desk)
    {
        if (!desk.TryGetProperty("names", out var names) || names.ValueKind != JsonValueKind.Array)
        {
            Body.Children.Add(Line("名单未到", 13, true));
            return;
        }
        var strategy = TextOf(desk, "strategy");
        var order = strategy;
        foreach (var row in names.EnumerateArray())
        {
            order += "|" + TextOf(row, "symbol");
        }
        if (_listHost == null || _listKey != order)
        {
            _listKey = order;
            _listRows.Clear();
            _listHost = new StackPanel();
            foreach (var row in names.EnumerateArray())
            {
                var symbol = TextOf(row, "symbol");
                var ui = NameRow.Build(this, row);
                _listRows[symbol] = ui;
                _listHost.Children.Add(ui.Root);
            }
        }
        else
        {
            foreach (var row in names.EnumerateArray())
            {
                if (_listRows.TryGetValue(TextOf(row, "symbol"), out var ui))
                {
                    ui.Apply(this, row);
                }
            }
        }
        Body.Children.Add(_listHost);
    }

    private StackPanel Meter(int progress)
    {
        var filled = progress / 25;
        var bar = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center };
        for (var step = 1; step <= 4; step++)
        {
            bar.Children.Add(new Border
            {
                Width = 16,
                Height = 6,
                Margin = new Thickness(0, 0, 3, 0),
                Background = step <= filled ? _gold : _dim,
            });
        }
        return bar;
    }

    private void FillWave(Canvas canvas, JsonElement row)
    {
        canvas.Children.Clear();
        var values = new List<double>();
        if (row.TryGetProperty("wave", out var wave) && wave.ValueKind == JsonValueKind.Array)
        {
            foreach (var point in wave.EnumerateArray())
            {
                var text = point.ValueKind == JsonValueKind.String ? point.GetString() : point.ToString();
                if (double.TryParse(
                    text,
                    System.Globalization.NumberStyles.Number,
                    System.Globalization.CultureInfo.InvariantCulture,
                    out var number))
                {
                    values.Add(number);
                }
            }
        }
        if (values.Count < 2)
        {
            canvas.Children.Add(new Line
            {
                X1 = 4,
                X2 = 84,
                Y1 = 14,
                Y2 = 14,
                Stroke = _dim,
                StrokeThickness = 1,
            });
            return;
        }
        var low = values.Min();
        var high = values.Max();
        var span = Math.Max(high - low, 0.01);
        var line = new Polyline { Stroke = _gold, StrokeThickness = 1.6 };
        for (var index = 0; index < values.Count; index++)
        {
            var x = 4 + index * (80.0 / (values.Count - 1));
            var y = 24 - ((values[index] - low) / span) * 20;
            line.Points.Add(new Point(x, y));
        }
        canvas.Children.Add(line);
    }

    private sealed class NameRow
    {
        public Border Root { get; }
        private readonly TextBlock _price;
        private readonly TextBlock _percent;
        private readonly TextBlock _gate;
        private readonly Border[] _steps;
        private readonly Canvas _canvas;

        private NameRow(
            Border root,
            TextBlock price,
            TextBlock percent,
            TextBlock gate,
            Border[] steps,
            Canvas canvas)
        {
            Root = root;
            _price = price;
            _percent = percent;
            _gate = gate;
            _steps = steps;
            _canvas = canvas;
        }

        public static NameRow Build(MainWindow window, JsonElement row)
        {
            var industry = TextOf(row, "industry");
            var title = industry.Length == 0
                ? TextOf(row, "symbol")
                : $"{TextOf(row, "symbol")} · {industry}";
            var head = new StackPanel();
            head.Children.Add(window.Line(title, 14, false));
            var gate = window.Line("", 11, true);
            head.Children.Add(gate);
            var steps = new Border[4];
            var meter = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                VerticalAlignment = VerticalAlignment.Center,
            };
            for (var step = 0; step < 4; step++)
            {
                steps[step] = new Border
                {
                    Width = 16,
                    Height = 6,
                    Margin = new Thickness(0, 0, 3, 0),
                };
                meter.Children.Add(steps[step]);
            }
            var percent = window.Line("0%", 12, true);
            percent.Margin = new Thickness(6, 0, 0, 0);
            percent.VerticalAlignment = VerticalAlignment.Center;
            meter.Children.Add(percent);
            head.Children.Add(meter);
            var price = window.Line("—", 14, false);
            price.VerticalAlignment = VerticalAlignment.Center;
            var canvas = new Canvas { Width = 88, Height = 28 };
            var waveBox = new Border
            {
                Width = 96,
                Height = 34,
                BorderBrush = window._dim,
                BorderThickness = new Thickness(1),
                Background = window._card,
                Child = canvas,
                VerticalAlignment = VerticalAlignment.Center,
            };
            var grid = new Grid { Margin = new Thickness(0, 6, 0, 0) };
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(150) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(100) });
            Grid.SetColumn(head, 0);
            Grid.SetColumn(price, 1);
            Grid.SetColumn(waveBox, 2);
            grid.Children.Add(head);
            grid.Children.Add(price);
            grid.Children.Add(waveBox);
            var root = new Border { BorderBrush = window._gold, Child = grid };
            var built = new NameRow(root, price, percent, gate, steps, canvas);
            built.Apply(window, row);
            return built;
        }

        public void Apply(MainWindow window, JsonElement row)
        {
            var progress = 0;
            if (row.TryGetProperty("progress", out var value) && value.TryGetInt32(out var parsed))
            {
                progress = parsed;
            }
            var price = TextOf(row, "price");
            _price.Text = price.Length == 0 ? "—" : price;
            _percent.Text = $"{progress}%";
            _percent.Foreground = progress < 100 ? window._muted : window._ink;
            _gate.Text = TextOf(row, "gate");
            var filled = progress / 25;
            for (var step = 0; step < _steps.Length; step++)
            {
                _steps[step].Background = step < filled ? window._gold : window._dim;
            }
            Root.BorderThickness = new Thickness(progress == 100 ? 2 : 0, 0, 0, 0);
            Root.Padding = new Thickness(progress == 100 ? 8 : 0, 0, 0, 0);
            window.FillWave(_canvas, row);
        }
    }

    private void AddCard(string title, string detail)
    {
        var panel = new StackPanel { Margin = new Thickness(0, 8, 0, 0) };
        panel.Children.Add(Line(title, 15, false));
        panel.Children.Add(Line(detail, 13, true));
        Body.Children.Add(new Border
        {
            Background = _card,
            Padding = new Thickness(10),
            Margin = new Thickness(0, 8, 0, 0),
            Child = panel,
        });
    }

    private void AddRows(JsonElement owner, string name, string empty, Func<JsonElement, string> format)
    {
        if (!owner.TryGetProperty(name, out var rows) || rows.GetArrayLength() == 0)
        {
            Body.Children.Add(Line(empty, 13, true));
            return;
        }
        foreach (var row in rows.EnumerateArray())
        {
            Body.Children.Add(Line(format(row), 13, false));
        }
    }

    private Border Stat(string value, string label)
    {
        var panel = new StackPanel();
        panel.Children.Add(Line(value, 18, false));
        panel.Children.Add(Line(label, 12, true));
        return new Border
        {
            Background = _card,
            Padding = new Thickness(10),
            Margin = new Thickness(0, 0, 8, 8),
            Child = panel,
        };
    }

    private TextBlock Line(string text, double size, bool muted)
    {
        return new TextBlock
        {
            Text = text,
            FontSize = size,
            Foreground = muted ? _muted : _ink,
            TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(0, 2, 0, 2),
        };
    }

    private void DismissNotice(object sender, RoutedEventArgs args)
    {
        Notice.Visibility = Visibility.Collapsed;
    }

    private static int Count(JsonElement owner, string name)
    {
        return owner.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.Array
            ? value.GetArrayLength()
            : 0;
    }

    private static string TextOf(JsonElement owner, string name)
    {
        if (!owner.TryGetProperty(name, out var value) || value.ValueKind == JsonValueKind.Null)
        {
            return "";
        }
        return value.ValueKind == JsonValueKind.String ? value.GetString() ?? "" : value.ToString();
    }

    private static SolidColorBrush Freeze(Color color)
    {
        var brush = new SolidColorBrush(color);
        brush.Freeze();
        return brush;
    }
}
