package com.bstocks.paperdesk

import android.Manifest
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject

// ==========================================
// Financial Terminal Design System Colors
// ==========================================
private object Theme {
    val Bg = Color(0xFF0A0C10)
    val Surface = Color(0xFF12151C)
    val SurfaceElevated = Color(0xFF191D27)
    val Card = Color(0xFF161A23)
    val CardBorder = Color(0xFF242A38)
    val CardBorderActive = Color(0xFF3B455B)

    val Gold = Color(0xFFE5C07B)
    val GoldBright = Color(0xFFFFD580)
    val GoldDim = Color(0xFF2E2618)
    val GoldGlow = Color(0x33E5C07B)

    val Mint = Color(0xFF10B981)
    val MintDim = Color(0xFF0B3326)
    val Coral = Color(0xFFF43F5E)
    val CoralDim = Color(0xFF3D141E)
    val Sky = Color(0xFF38BDF8)
    val SkyDim = Color(0xFF0F3144)

    val TextPrimary = Color(0xFFF8FAFC)
    val TextSecondary = Color(0xFF94A3B8)
    val TextMuted = Color(0xFF64748B)
}

private val Tabs = listOf("S1", "S2", "S3", "S4", "S5", "状态")
private const val DefaultBase = "http://43.163.203.127:8765"
private const val LocalBase = "http://127.0.0.1:8765"

private val StrategyMeta = mapOf(
    "S1" to Pair("S1 残差趋势", "Kalman+RLS日内波动率归"),
    "S2" to Pair("S2 VWAP 回踩", "VWAP/RSI(6)+多分类校准"),
    "S3" to Pair("S3 开盘区间突破", "ORB+RVOL+低延迟"),
    "S4" to Pair("S4 超卖均值反弹", "VWAP/RSI(6)"),
    "S5" to Pair("S5 尾盘极端反转", "尾盘因果反转模型"),
)

private data class TabSpec(val id: String, val code: String, val name: String)
private val TabSpecs = listOf(
    TabSpec("S1", "S1", "残差趋势"),
    TabSpec("S2", "S2", "VWAP回踩"),
    TabSpec("S3", "S3", "开盘突破"),
    TabSpec("S4", "S4", "均值反弹"),
    TabSpec("S5", "S5", "极端反转"),
    TabSpec("状态", "状态", "系统监控")
)

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        if (Build.VERSION.SDK_INT >= 33) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
        androidx.core.content.ContextCompat.startForegroundService(
            this,
            Intent(this, DeskWatcherService::class.java),
        )
        askToStayAlive()
        setContent { PaperDeskApp(this) }
    }

    private fun askToStayAlive() {
        val prefs = getSharedPreferences("paperdesk", MODE_PRIVATE)
        if (prefs.getBoolean("battery_asked", false)) {
            return
        }
        val power = getSystemService(PowerManager::class.java)
        if (power.isIgnoringBatteryOptimizations(packageName)) {
            prefs.edit().putBoolean("battery_asked", true).apply()
            return
        }
        prefs.edit().putBoolean("battery_asked", true).apply()
        startActivity(
            Intent(
                Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                Uri.parse("package:$packageName"),
            ),
        )
    }
}

@Composable
private fun PaperDeskApp(context: Context) {
    val prefs = remember { context.getSharedPreferences("paperdesk", Context.MODE_PRIVATE) }
    var base by remember { mutableStateOf(prefs.getString("base_japan", DefaultBase) ?: DefaultBase) }
    var draft by remember { mutableStateOf(base) }
    var tab by remember { mutableStateOf("S1") }
    var inspectingRow by remember { mutableStateOf<JSONObject?>(null) }
    val watchlist = remember { mutableStateListOf<String>() }
    val link by DeskBus.link.collectAsState()
    val payload = link.payload
    val failure = link.failure
    val alert = link.alert
    val lastPingMs = link.latencyMs

    LaunchedEffect(Unit) {
        val savedPins = prefs.getStringSet("watchlist_symbols", emptySet()) ?: emptySet()
        watchlist.addAll(savedPins)
    }

    Box(
        Modifier
            .fillMaxSize()
            .background(Theme.Bg)
    ) {
        Column(Modifier.fillMaxSize()) {
            TopAppBar(
                isConnected = payload != null && failure == null,
                status = when {
                    payload != null && failure == null -> "实时行情流已同步"
                    failure == null -> "正在连接日本服务器"
                    payload != null -> "连接中断，保留上次盘面"
                    else -> "后端连接中断"
                },
                pingMs = lastPingMs,
                onRefresh = { DeskBus.dismissAlert() }
            )

            AnimatedVisibility(
                visible = alert != null,
                enter = slideInVertically() + fadeIn(),
                exit = slideOutVertically() + fadeOut()
            ) {
                alert?.let {
                    NotificationBanner(it) { DeskBus.dismissAlert() }
                }
            }

            Box(
                Modifier
                    .weight(1f)
                    .fillMaxWidth()
            ) {
                val state = payload
                if (state == null) {
                    DisconnectedView(
                        base = base,
                        failure = failure,
                        connecting = failure == null,
                        onSwitchToLocal = {
                            base = LocalBase
                            draft = LocalBase
                            prefs.edit().putString("base_japan", LocalBase).apply()
                        },
                        onSwitchToJapan = {
                            base = DefaultBase
                            draft = DefaultBase
                            prefs.edit().putString("base_japan", DefaultBase).apply()
                        }
                    )
                } else if (tab == "状态") {
                    StatusPage(
                        state = state,
                        currentBase = base,
                        draft = draft,
                        lastPingMs = lastPingMs,
                        onDraft = { draft = it },
                        onSave = {
                            val next = draft.trim().trimEnd('/')
                            prefs.edit().putString("base_japan", next).apply()
                            base = next
                            Toast.makeText(context, "已更新连接地址", Toast.LENGTH_SHORT).show()
                        },
                        onSelectStrategy = { tab = it }
                    )
                } else {
                    DeskPage(
                        state = state,
                        strategy = tab,
                        watchlist = watchlist,
                        onToggleWatchlist = { sym ->
                            if (watchlist.contains(sym)) watchlist.remove(sym) else watchlist.add(sym)
                            prefs.edit().putStringSet("watchlist_symbols", watchlist.toSet()).apply()
                        },
                        onInspectStock = { inspectingRow = it }
                    )
                }
            }

            BottomNavBar(
                activeTab = tab,
                state = payload,
                onSelectTab = { tab = it }
            )
        }

        inspectingRow?.let { row ->
            StockDetailDialog(
                row = row,
                isWatchlist = watchlist.contains(text(row, "symbol")),
                onToggleWatchlist = {
                    val sym = text(row, "symbol")
                    if (watchlist.contains(sym)) watchlist.remove(sym) else watchlist.add(sym)
                    prefs.edit().putStringSet("watchlist_symbols", watchlist.toSet()).apply()
                },
                onCopy = { sym ->
                    val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                    clipboard.setPrimaryClip(ClipData.newPlainText("symbol", sym))
                    Toast.makeText(context, "代码 $sym 已复制到剪贴板", Toast.LENGTH_SHORT).show()
                },
                onDismiss = { inspectingRow = null }
            )
        }
    }
}

// ==========================================
// Top Header & Status Bar
// ==========================================
@Composable
private fun TopAppBar(
    isConnected: Boolean,
    status: String,
    pingMs: Long,
    onRefresh: () -> Unit,
) {
    val infiniteTransition = rememberInfiniteTransition(label = "pulse")
    val pulseAlpha by infiniteTransition.animateFloat(
        initialValue = 0.4f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(
            animation = tween(1000, easing = FastOutSlowInEasing),
            repeatMode = RepeatMode.Reverse
        ),
        label = "alpha"
    )

    Surface(
        modifier = Modifier.fillMaxWidth(),
        color = Theme.Surface,
        shadowElevation = 4.dp
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .statusBarsPadding()
                .padding(horizontal = 14.dp, vertical = 6.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(
                    modifier = Modifier
                        .size(10.dp)
                        .background(
                            if (isConnected) Theme.Mint.copy(alpha = pulseAlpha) else Theme.Coral,
                            CircleShape
                        )
                )
                Spacer(Modifier.width(8.dp))
                Column {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(
                            "V2.2 QUANT DESK",
                            color = Theme.TextPrimary,
                            fontSize = 15.sp,
                            fontWeight = FontWeight.Bold,
                            fontFamily = FontFamily.Monospace,
                            letterSpacing = 0.5.sp
                        )
                        Spacer(Modifier.width(6.dp))
                        Box(
                            Modifier
                                .background(Theme.GoldDim, RoundedCornerShape(4.dp))
                                .border(1.dp, Theme.Gold.copy(alpha = 0.4f), RoundedCornerShape(4.dp))
                                .padding(horizontal = 5.dp, vertical = 1.dp)
                        ) {
                            Text("v1.27", color = Theme.Gold, fontSize = 10.sp, fontWeight = FontWeight.Bold)
                        }
                    }
                    Text(
                        status,
                        color = if (isConnected) Theme.TextSecondary else Theme.Coral,
                        fontSize = 11.sp
                    )
                }
            }

            Row(verticalAlignment = Alignment.CenterVertically) {
                if (isConnected && pingMs >= 0) {
                    Box(
                        Modifier
                            .background(Theme.SurfaceElevated, RoundedCornerShape(12.dp))
                            .border(1.dp, Theme.CardBorder, RoundedCornerShape(12.dp))
                            .padding(horizontal = 8.dp, vertical = 4.dp)
                    ) {
                        Text(
                            "⚡ ${pingMs}ms",
                            color = if (pingMs < 100) Theme.Mint else if (pingMs < 300) Theme.Gold else Theme.Coral,
                            fontSize = 11.sp,
                            fontWeight = FontWeight.Medium
                        )
                    }
                    Spacer(Modifier.width(8.dp))
                }

                Box(
                    Modifier
                        .size(32.dp)
                        .background(Theme.SurfaceElevated, CircleShape)
                        .border(1.dp, Theme.CardBorder, CircleShape)
                        .clickable(onClick = onRefresh),
                    contentAlignment = Alignment.Center
                ) {
                    Text("⟳", color = Theme.TextSecondary, fontSize = 16.sp, fontWeight = FontWeight.Bold)
                }
            }
        }
    }
}

// ==========================================
// Floating Notification Banner
// ==========================================
@Composable
private fun NotificationBanner(text: String, onDismiss: () -> Unit) {
    Box(
        Modifier
            .fillMaxWidth()
            .padding(horizontal = 16.dp, vertical = 8.dp)
    ) {
        Row(
            Modifier
                .fillMaxWidth()
                .shadow(8.dp, RoundedCornerShape(10.dp))
                .background(Theme.GoldDim, RoundedCornerShape(10.dp))
                .border(1.dp, Theme.Gold.copy(alpha = 0.8f), RoundedCornerShape(10.dp))
                .clickable(onClick = onDismiss)
                .padding(14.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween
        ) {
            Column(Modifier.weight(1f)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("🎯 模拟信号提醒", color = Theme.GoldBright, fontSize = 12.sp, fontWeight = FontWeight.Bold)
                    Spacer(Modifier.width(8.dp))
                    Text("点此关闭", color = Theme.TextMuted, fontSize = 11.sp)
                }
                Spacer(Modifier.height(4.dp))
                Text(text, color = Theme.TextPrimary, fontSize = 14.sp, fontWeight = FontWeight.Medium)
            }
            Text("✕", color = Theme.Gold, fontSize = 16.sp, modifier = Modifier.padding(start = 12.dp))
        }
    }
}

// ==========================================
// Bottom Navigation Bar
// ==========================================
@Composable
private fun BottomNavBar(
    activeTab: String,
    state: JSONObject?,
    onSelectTab: (String) -> Unit
) {
    val desks = state?.optJSONArray("desks") ?: JSONArray()
    val activeDesks = remember(state) {
        val map = mutableMapOf<String, Boolean>()
        for (i in 0 until desks.length()) {
            val d = desks.optJSONObject(i) ?: continue
            val strat = text(d, "strategy")
            val hasPositions = (d.optJSONArray("positions")?.length() ?: 0) > 0
            val hasWorking = (d.optJSONArray("working")?.length() ?: 0) > 0
            map[strat] = hasPositions || hasWorking
        }
        map
    }

    Surface(
        modifier = Modifier.fillMaxWidth(),
        color = Theme.Surface,
        shadowElevation = 8.dp
    ) {
        Row(
            Modifier
                .fillMaxWidth()
                .navigationBarsPadding()
                .padding(horizontal = 4.dp, vertical = 4.dp)
        ) {
            TabSpecs.forEach { spec ->
                val selected = spec.id == activeTab
                val hasActivity = activeDesks[spec.id] == true

                val animatedBg by animateColorAsState(
                    if (selected) Theme.GoldGlow else Color.Transparent,
                    animationSpec = tween(200, easing = LinearEasing),
                    label = "tabBg"
                )

                Column(
                    Modifier
                        .weight(1f)
                        .clip(RoundedCornerShape(8.dp))
                        .background(animatedBg)
                        .clickable { onSelectTab(spec.id) }
                        .padding(vertical = 6.dp),
                    horizontalAlignment = Alignment.CenterHorizontally
                ) {
                    Box(contentAlignment = Alignment.TopEnd) {
                        Text(
                            spec.code,
                            color = if (selected) Theme.GoldBright else Theme.TextSecondary,
                            fontSize = 13.sp,
                            fontWeight = if (selected) FontWeight.Bold else FontWeight.Medium
                        )
                        if (hasActivity && !selected) {
                            Box(
                                Modifier
                                    .padding(start = 16.dp)
                                    .size(5.dp)
                                    .background(Theme.Mint, CircleShape)
                            )
                        }
                    }
                    Text(
                        spec.name,
                        color = if (selected) Theme.Gold else Theme.TextMuted,
                        fontSize = 10.sp,
                        fontWeight = if (selected) FontWeight.Bold else FontWeight.Normal,
                        maxLines = 1
                    )
                    Spacer(Modifier.height(2.dp))
                    Box(
                        Modifier
                            .width(16.dp)
                            .height(2.dp)
                            .background(
                                if (selected) Theme.Gold else Color.Transparent,
                                RoundedCornerShape(1.dp)
                            )
                    )
                }
            }
        }
    }
}

// ==========================================
// Strategy Desk Page (S1 - S5)
// ==========================================
@Composable
private fun DeskPage(
    state: JSONObject,
    strategy: String,
    watchlist: List<String>,
    onToggleWatchlist: (String) -> Unit,
    onInspectStock: (JSONObject) -> Unit
) {
    val desks = state.optJSONArray("desks") ?: JSONArray()
    val desk = (0 until desks.length()).mapNotNull { desks.optJSONObject(it) }.firstOrNull { text(it, "strategy") == strategy }
    if (desk == null) {
        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Text("等待 $strategy 策略就绪...", color = Theme.TextSecondary)
        }
        return
    }

    val meta = StrategyMeta[strategy] ?: Pair("量化策略", "核心模型")
    val positions = desk.optJSONArray("positions") ?: JSONArray()
    val working = desk.optJSONArray("working") ?: JSONArray()
    val rawNames = desk.optJSONArray("names") ?: JSONArray()
    val halted = desk.optBoolean("halt")
    val runtime = text(desk, "runtime")
    val reason = text(desk, "reason")

    var searchQuery by remember { mutableStateOf("") }
    var filterType by remember { mutableStateOf("ALL") } // ALL, ETF, STOCK, READY, WATCHLIST
    var sortOrder by remember { mutableStateOf("PROGRESS") } // PROGRESS, PRICE, SYMBOL
    var showHoldingsOrders by remember { mutableStateOf(false) }

    val allList = remember(rawNames) {
        (0 until rawNames.length()).mapNotNull { rawNames.optJSONObject(it) }
    }

    val filteredList = remember(allList, searchQuery, filterType, sortOrder, watchlist) {
        var res = allList
        if (searchQuery.isNotBlank()) {
            val q = searchQuery.trim().uppercase()
            res = res.filter { text(it, "symbol").contains(q) || text(it, "industry").contains(q) }
        }
        when (filterType) {
            "ETF" -> res = res.filter { text(it, "kind") == "ETF" }
            "STOCK" -> res = res.filter { text(it, "kind") != "ETF" }
            "READY" -> res = res.filter { it.optInt("progress") >= 100 }
            "WATCHLIST" -> res = res.filter { watchlist.contains(text(it, "symbol")) }
        }
        when (sortOrder) {
            "PRICE" -> res.sortedByDescending { text(it, "price").toDoubleOrNull() ?: 0.0 }
            "SYMBOL" -> res.sortedBy { text(it, "symbol") }
            else -> res.sortedWith(
                compareByDescending<JSONObject> { watchlist.contains(text(it, "symbol")) }
                    .thenByDescending { it.optInt("progress") }
            )
        }
    }

    LazyColumn(
        Modifier
            .fillMaxSize()
            .padding(horizontal = 14.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        item {
            Spacer(Modifier.height(4.dp))
            StrategyCockpit(
                title = meta.first,
                subtitle = meta.second,
                runtime = runtime,
                reason = reason,
                halted = halted,
                equity = text(desk, "equity"),
                cash = text(desk, "cash"),
                posCount = positions.length(),
                orderCount = working.length(),
                isExpanded = showHoldingsOrders,
                onToggleExpand = { showHoldingsOrders = !showHoldingsOrders }
            )
        }

        if (showHoldingsOrders && (positions.length() > 0 || working.length() > 0)) {
            if (positions.length() > 0) {
                item {
                    SectionHeader(
                        title = "当前持仓",
                        count = positions.length(),
                        badge = "${positions.length()} 活跃"
                    )
                }
                items(positions.length()) { index ->
                    PositionCard(positions.getJSONObject(index))
                }
            }

            if (working.length() > 0) {
                item {
                    SectionHeader(
                        title = "未成交挂单",
                        count = working.length(),
                        badge = "${working.length()} 挂单中"
                    )
                }
                items(working.length()) { index ->
                    WorkingOrderCard(working.getJSONObject(index))
                }
            }
        }

        item {
            Column {
                Row(
                    Modifier
                        .fillMaxWidth()
                        .padding(top = 2.dp, bottom = 4.dp),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(
                            "候选标的池",
                            color = Theme.TextPrimary,
                            fontSize = 14.sp,
                            fontWeight = FontWeight.Bold
                        )
                        Spacer(Modifier.width(6.dp))
                        Text(
                            "(${filteredList.size})",
                            color = Theme.Gold,
                            fontSize = 12.sp,
                            fontWeight = FontWeight.Bold
                        )
                        Spacer(Modifier.width(6.dp))
                        Text(
                            "11 ETF + 20 个股",
                            color = Theme.TextMuted,
                            fontSize = 10.sp
                        )
                    }

                    Box(
                        Modifier
                            .background(Theme.MintDim, RoundedCornerShape(4.dp))
                            .border(1.dp, Theme.Mint.copy(alpha = 0.4f), RoundedCornerShape(4.dp))
                            .padding(horizontal = 6.dp, vertical = 2.dp)
                    ) {
                        Text(
                            "100% 触发模拟成交",
                            color = Theme.Mint,
                            fontSize = 10.sp,
                            fontWeight = FontWeight.Medium
                        )
                    }
                }

                CandidateFilterToolbar(
                    searchQuery = searchQuery,
                    filterType = filterType,
                    sortOrder = sortOrder,
                    onSearchChange = { searchQuery = it },
                    onFilterChange = { filterType = it },
                    onSortToggle = {
                        sortOrder = when (sortOrder) {
                            "PROGRESS" -> "PRICE"
                            "PRICE" -> "SYMBOL"
                            else -> "PROGRESS"
                        }
                    }
                )
            }
        }

        if (filteredList.isEmpty()) {
            item {
                EmptyStateCard("未找到符合筛选条件的候选标的")
            }
        } else {
            items(filteredList.size) { index ->
                val row = filteredList[index]
                val sym = text(row, "symbol")
                CandidateStockCard(
                    row = row,
                    isStarred = watchlist.contains(sym),
                    onToggleStar = { onToggleWatchlist(sym) },
                    onClick = { onInspectStock(row) }
                )
            }
        }

        item {
            Spacer(Modifier.height(16.dp))
        }
    }
}

// ==========================================
// Strategy Integrated Cockpit (Ultra-Compact)
// ==========================================
@Composable
private fun StrategyCockpit(
    title: String,
    subtitle: String,
    runtime: String,
    reason: String,
    halted: Boolean,
    equity: String,
    cash: String,
    posCount: Int,
    orderCount: Int,
    isExpanded: Boolean,
    onToggleExpand: () -> Unit
) {
    Column(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(10.dp))
            .background(Theme.Surface)
            .border(1.dp, Theme.CardBorder, RoundedCornerShape(10.dp))
            .padding(horizontal = 12.dp, vertical = 9.dp)
    ) {
        // Row 1: Strategy Name + Subtitle on Left, Status Tags on Right
        Row(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Column(Modifier.weight(1f, fill = false)) {
                Text(title, color = Theme.TextPrimary, fontSize = 15.sp, fontWeight = FontWeight.Bold)
                Spacer(Modifier.height(1.dp))
                Text(
                    subtitle,
                    color = Theme.Gold,
                    fontSize = 11.sp,
                    fontWeight = FontWeight.Medium,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
            }

            Spacer(Modifier.width(8.dp))

            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(5.dp)) {
                if (reason.isNotBlank()) {
                    Box(
                        Modifier
                            .background(Theme.Card, RoundedCornerShape(4.dp))
                            .border(1.dp, Theme.CardBorder, RoundedCornerShape(4.dp))
                            .padding(horizontal = 6.dp, vertical = 2.dp)
                    ) {
                        Text(
                            reason,
                            color = Theme.TextSecondary,
                            fontSize = 10.sp,
                            fontWeight = FontWeight.Medium
                        )
                    }
                }

                Box(
                    Modifier
                        .background(if (halted) Theme.CoralDim else Theme.MintDim, RoundedCornerShape(4.dp))
                        .border(1.dp, if (halted) Theme.Coral else Theme.Mint, RoundedCornerShape(4.dp))
                        .padding(horizontal = 6.dp, vertical = 2.dp)
                ) {
                    Text(
                        if (halted) "停开" else runtime.uppercase(),
                        color = if (halted) Theme.Coral else Theme.Mint,
                        fontSize = 10.sp,
                        fontWeight = FontWeight.Bold
                    )
                }
            }
        }

        Spacer(Modifier.height(7.dp))
        HorizontalDivider(color = Theme.CardBorder.copy(alpha = 0.5f), thickness = 0.5.dp)
        Spacer(Modifier.height(7.dp))

        // Row 2: 4-Column Compact Metric Strip
        Row(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Column(Modifier.weight(1f)) {
                Text("权益 USDC", color = Theme.TextMuted, fontSize = 10.sp)
                Spacer(Modifier.height(1.dp))
                Text(
                    if (equity.isBlank()) "$1,600" else "$$equity",
                    color = Theme.Gold,
                    fontSize = 13.sp,
                    fontWeight = FontWeight.Bold,
                    fontFamily = FontFamily.Monospace,
                    maxLines = 1
                )
            }

            Column(Modifier.weight(1f)) {
                Text("现金 USDC", color = Theme.TextMuted, fontSize = 10.sp)
                Spacer(Modifier.height(1.dp))
                Text(
                    if (cash.isBlank()) "$1,600" else "$$cash",
                    color = Theme.Sky,
                    fontSize = 13.sp,
                    fontWeight = FontWeight.Bold,
                    fontFamily = FontFamily.Monospace,
                    maxLines = 1
                )
            }

            Column(Modifier.weight(0.85f)) {
                Text("持仓标的", color = Theme.TextMuted, fontSize = 10.sp)
                Spacer(Modifier.height(1.dp))
                Text(
                    "$posCount 支",
                    color = if (posCount > 0) Theme.Mint else Theme.TextPrimary,
                    fontSize = 13.sp,
                    fontWeight = FontWeight.Bold,
                    maxLines = 1
                )
            }

            Column(Modifier.weight(0.95f), horizontalAlignment = Alignment.End) {
                Text("开仓门禁", color = Theme.TextMuted, fontSize = 10.sp)
                Spacer(Modifier.height(1.dp))
                Text(
                    if (halted) "停开保护" else "正常评估",
                    color = if (halted) Theme.Coral else Theme.Mint,
                    fontSize = 13.sp,
                    fontWeight = FontWeight.Bold,
                    maxLines = 1
                )
            }
        }

        // Row 3: Slim Status Line / Active trading banner
        val hasActiveTrading = posCount > 0 || orderCount > 0
        Spacer(Modifier.height(6.dp))
        if (!hasActiveTrading) {
            Row(
                Modifier
                    .fillMaxWidth()
                    .background(Theme.Card, RoundedCornerShape(4.dp))
                    .padding(horizontal = 8.dp, vertical = 3.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.Center
            ) {
                Text(
                    "🛡️ 空仓 (0) · 无在途挂单 (0) · 风险中性",
                    color = Theme.TextMuted,
                    fontSize = 10.sp
                )
            }
        } else {
            Row(
                Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(4.dp))
                    .background(Theme.GoldDim)
                    .border(1.dp, Theme.Gold.copy(alpha = 0.5f), RoundedCornerShape(4.dp))
                    .clickable(onClick = onToggleExpand)
                    .padding(horizontal = 8.dp, vertical = 4.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                Text(
                    "📊 活跃在途：持仓 $posCount 笔 · 挂单 $orderCount 笔",
                    color = Theme.GoldBright,
                    fontSize = 11.sp,
                    fontWeight = FontWeight.Bold
                )
                Text(
                    if (isExpanded) "收起 ▲" else "展开明细 ▼",
                    color = Theme.Gold,
                    fontSize = 10.sp,
                    fontWeight = FontWeight.Medium
                )
            }
        }
    }
}

// ==========================================
// Candidate Pool Filter & Stock Card
// ==========================================
@Composable
private fun CandidateFilterToolbar(
    searchQuery: String,
    filterType: String,
    sortOrder: String,
    onSearchChange: (String) -> Unit,
    onFilterChange: (String) -> Unit,
    onSortToggle: () -> Unit
) {
    Column(Modifier.fillMaxWidth().padding(bottom = 8.dp)) {
        Row(
            Modifier
                .fillMaxWidth()
                .background(Theme.Surface, RoundedCornerShape(8.dp))
                .border(1.dp, Theme.CardBorder, RoundedCornerShape(8.dp))
                .padding(horizontal = 10.dp, vertical = 6.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text("🔍", fontSize = 12.sp)
            Spacer(Modifier.width(8.dp))
            BasicTextField(
                value = searchQuery,
                onValueChange = onSearchChange,
                singleLine = true,
                textStyle = TextStyle(color = Theme.TextPrimary, fontSize = 13.sp),
                cursorBrush = SolidColor(Theme.Gold),
                modifier = Modifier.weight(1f),
                decorationBox = { innerTextField ->
                    if (searchQuery.isEmpty()) {
                        Text("搜索代码或行业 (如 NVDA, XLE)...", color = Theme.TextMuted, fontSize = 13.sp)
                    }
                    innerTextField()
                }
            )
            if (searchQuery.isNotEmpty()) {
                Text(
                    "✕",
                    color = Theme.TextMuted,
                    fontSize = 12.sp,
                    modifier = Modifier.clickable { onSearchChange("") }
                )
            }
        }

        Spacer(Modifier.height(8.dp))

        Row(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            LazyRow(horizontalArrangement = Arrangement.spacedBy(6.dp), modifier = Modifier.weight(1f)) {
                items(listOf("ALL" to "全部", "ETF" to "行业ETF", "STOCK" to "个股", "READY" to "100%信号", "WATCHLIST" to "⭐关注")) { item ->
                    val selected = filterType == item.first
                    Box(
                        Modifier
                            .clip(RoundedCornerShape(6.dp))
                            .background(if (selected) Theme.Gold else Theme.Surface)
                            .border(1.dp, if (selected) Theme.Gold else Theme.CardBorder, RoundedCornerShape(6.dp))
                            .clickable { onFilterChange(item.first) }
                            .padding(horizontal = 8.dp, vertical = 4.dp)
                    ) {
                        Text(
                            item.second,
                            color = if (selected) Theme.Bg else Theme.TextSecondary,
                            fontSize = 11.sp,
                            fontWeight = if (selected) FontWeight.Bold else FontWeight.Medium
                        )
                    }
                }
            }

            Spacer(Modifier.width(8.dp))

            Box(
                Modifier
                    .clip(RoundedCornerShape(6.dp))
                    .background(Theme.SurfaceElevated)
                    .border(1.dp, Theme.CardBorder, RoundedCornerShape(6.dp))
                    .clickable(onClick = onSortToggle)
                    .padding(horizontal = 8.dp, vertical = 4.dp)
            ) {
                val sortLabel = when (sortOrder) {
                    "PRICE" -> "价格↓"
                    "SYMBOL" -> "代码A-Z"
                    else -> "达成度↓"
                }
                Text("排序: $sortLabel", color = Theme.Gold, fontSize = 11.sp, fontWeight = FontWeight.Medium)
            }
        }
    }
}

@Composable
private fun CandidateStockCard(
    row: JSONObject,
    isStarred: Boolean,
    onToggleStar: () -> Unit,
    onClick: () -> Unit
) {
    val progress = row.optInt("progress")
    val price = text(row, "price").ifEmpty { "—" }
    val symbol = text(row, "symbol")
    val industry = text(row, "industry")
    val gate = text(row, "gate")
    val wave = waveOf(row)
    val isReady = progress >= 100

    Row(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(10.dp))
            .background(if (isReady) Theme.GoldDim else Theme.Surface)
            .border(
                1.dp,
                if (isReady) Theme.Gold else if (isStarred) Theme.Gold.copy(alpha = 0.5f) else Theme.CardBorder,
                RoundedCornerShape(10.dp)
            )
            .clickable(onClick = onClick)
            .padding(horizontal = 12.dp, vertical = 9.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Column(Modifier.weight(1f)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    symbol,
                    color = Theme.TextPrimary,
                    fontSize = 16.sp,
                    fontWeight = FontWeight.Bold,
                    fontFamily = FontFamily.Monospace
                )
                if (industry.isNotBlank()) {
                    Spacer(Modifier.width(6.dp))
                    Box(
                        Modifier
                            .background(Theme.Card, RoundedCornerShape(4.dp))
                            .padding(horizontal = 5.dp, vertical = 1.dp)
                    ) {
                        Text(industry, color = Theme.TextSecondary, fontSize = 10.sp)
                    }
                }
                Spacer(Modifier.width(6.dp))
                Text(
                    if (isStarred) "★" else "☆",
                    color = if (isStarred) Theme.Gold else Theme.TextMuted,
                    fontSize = 14.sp,
                    modifier = Modifier.clickable(onClick = onToggleStar)
                )
            }

            Spacer(Modifier.height(4.dp))
            Text(
                if (gate.isBlank()) "正常观测中" else gate,
                color = if (isReady) Theme.Gold else Theme.TextMuted,
                fontSize = 11.sp,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis
            )

            Spacer(Modifier.height(6.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                repeat(4) { step ->
                    val filled = step < (progress / 25)
                    Box(
                        Modifier
                            .padding(end = 4.dp)
                            .size(width = 16.dp, height = 5.dp)
                            .background(
                                if (filled) Theme.Gold else Theme.CardBorder,
                                RoundedCornerShape(2.dp)
                            )
                    )
                }
                Text(
                    "$progress%",
                    color = if (isReady) Theme.GoldBright else Theme.Gold,
                    fontSize = 11.sp,
                    fontWeight = FontWeight.Bold,
                    modifier = Modifier.padding(start = 4.dp)
                )
            }
        }

        Column(horizontalAlignment = Alignment.End) {
            Text(
                if (price == "—") price else "$$price",
                color = Theme.TextPrimary,
                fontSize = 16.sp,
                fontWeight = FontWeight.Bold,
                fontFamily = FontFamily.Monospace
            )
            Spacer(Modifier.height(4.dp))
            SmoothSparkline(
                values = wave,
                modifier = Modifier
                    .size(width = 76.dp, height = 34.dp)
                    .clip(RoundedCornerShape(4.dp))
                    .background(Theme.Bg)
                    .border(1.dp, Theme.CardBorder, RoundedCornerShape(4.dp))
                    .padding(3.dp)
            )
        }
    }
}

// ==========================================
// Ultra-Smooth Cubic Bezier Sparkline
// ==========================================
@Composable
private fun SmoothSparkline(
    values: List<Float>,
    modifier: Modifier = Modifier
) {
    Canvas(modifier = modifier) {
        if (values.size < 2) {
            drawLine(
                color = Theme.TextMuted,
                start = Offset(0f, size.height / 2f),
                end = Offset(size.width, size.height / 2f),
                strokeWidth = 1.5f
            )
            return@Canvas
        }

        val low = values.min()
        val high = values.max()
        val span = (high - low).takeIf { it > 0.0001f } ?: 1f

        val isUp = values.last() >= values.first()
        val strokeColor = if (isUp) Theme.Mint else Theme.Coral

        val points = values.mapIndexed { index, value ->
            val x = size.width * index / (values.size - 1)
            val y = size.height - (size.height * (value - low) / span)
            Offset(x, y.coerceIn(2f, size.height - 2f))
        }

        val path = Path()
        val fillPath = Path()

        path.moveTo(points.first().x, points.first().y)
        fillPath.moveTo(points.first().x, size.height)
        fillPath.lineTo(points.first().x, points.first().y)

        for (i in 0 until points.size - 1) {
            val p0 = points[i]
            val p1 = points[i + 1]
            val cx = (p0.x + p1.x) / 2f
            path.cubicTo(cx, p0.y, cx, p1.y, p1.x, p1.y)
            fillPath.cubicTo(cx, p0.y, cx, p1.y, p1.x, p1.y)
        }

        fillPath.lineTo(points.last().x, size.height)
        fillPath.close()

        drawPath(
            path = fillPath,
            brush = Brush.verticalGradient(
                colors = listOf(strokeColor.copy(alpha = 0.35f), Color.Transparent),
                startY = 0f,
                endY = size.height
            )
        )

        drawPath(
            path = path,
            color = strokeColor,
            style = Stroke(
                width = 2f,
                cap = StrokeCap.Round,
                join = StrokeJoin.Round
            )
        )

        drawCircle(
            color = strokeColor,
            radius = 2.5.dp.toPx(),
            center = points.last()
        )
    }
}

// ==========================================
// Position & Order Cards
// ==========================================
@Composable
private fun PositionCard(pos: JSONObject) {
    val symbol = text(pos, "symbol")
    val qty = text(pos, "qty")
    val avg = text(pos, "avg")
    val mark = text(pos, "mark")
    val stop = text(pos, "stop")
    val tp = text(pos, "take_profit")

    Column(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(8.dp))
            .background(Theme.Surface)
            .border(1.dp, Theme.CardBorder, RoundedCornerShape(8.dp))
            .padding(10.dp)
    ) {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text(symbol, color = Theme.TextPrimary, fontSize = 15.sp, fontWeight = FontWeight.Bold)
            Text("$qty 股", color = Theme.Gold, fontSize = 14.sp, fontWeight = FontWeight.Medium)
        }
        Spacer(Modifier.height(4.dp))
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text("成本 $$avg · 现价 $$mark", color = Theme.TextSecondary, fontSize = 12.sp)
            if (stop.isNotBlank() || tp.isNotBlank()) {
                Text("止损 $$stop · 止盈 $$tp", color = Theme.TextMuted, fontSize = 11.sp)
            }
        }
    }
}

@Composable
private fun WorkingOrderCard(order: JSONObject) {
    val symbol = text(order, "symbol")
    val qty = text(order, "qty")
    val limit = text(order, "limit")

    Row(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(8.dp))
            .background(Theme.Surface)
            .border(1.dp, Theme.SkyDim, RoundedCornerShape(8.dp))
            .padding(10.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically
    ) {
        Column {
            Text(symbol, color = Theme.TextPrimary, fontSize = 15.sp, fontWeight = FontWeight.Bold)
            Text("限价买入挂单 · 等待成交", color = Theme.TextMuted, fontSize = 11.sp)
        }
        Text("$qty 股 @ $$limit", color = Theme.Sky, fontSize = 14.sp, fontWeight = FontWeight.Bold)
    }
}

// ==========================================
// Stock Inspection Dialog (Deep Inspection)
// ==========================================
@Composable
private fun StockDetailDialog(
    row: JSONObject,
    isWatchlist: Boolean,
    onToggleWatchlist: () -> Unit,
    onCopy: (String) -> Unit,
    onDismiss: () -> Unit
) {
    val symbol = text(row, "symbol")
    val industry = text(row, "industry")
    val kind = text(row, "kind")
    val price = text(row, "price").ifEmpty { "—" }
    val gate = text(row, "gate")
    val progress = row.optInt("progress")
    val wave = waveOf(row)

    Dialog(onDismissRequest = onDismiss) {
        Surface(
            modifier = Modifier
                .fillMaxWidth()
                .clip(RoundedCornerShape(16.dp)),
            color = Theme.Surface,
            border = androidx.compose.foundation.BorderStroke(1.dp, Theme.CardBorder)
        ) {
            Column(Modifier.padding(20.dp)) {
                Row(
                    Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Column {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text(symbol, color = Theme.TextPrimary, fontSize = 22.sp, fontWeight = FontWeight.Bold)
                            Spacer(Modifier.width(8.dp))
                            Box(
                                Modifier
                                    .background(Theme.Card, RoundedCornerShape(4.dp))
                                    .padding(horizontal = 6.dp, vertical = 2.dp)
                            ) {
                                Text(if (industry.isNotBlank()) industry else kind, color = Theme.Gold, fontSize = 11.sp)
                            }
                        }
                        Text("当前官方报价：$$price", color = Theme.TextSecondary, fontSize = 13.sp)
                    }

                    Box(
                        Modifier
                            .size(32.dp)
                            .background(Theme.Card, CircleShape)
                            .clickable(onClick = onDismiss),
                        contentAlignment = Alignment.Center
                    ) {
                        Text("✕", color = Theme.TextSecondary, fontSize = 14.sp)
                    }
                }

                Spacer(Modifier.height(16.dp))
                HorizontalDivider(color = Theme.CardBorder)
                Spacer(Modifier.height(14.dp))

                Text("形态达成进度", color = Theme.TextMuted, fontSize = 12.sp)
                Spacer(Modifier.height(6.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    repeat(4) { step ->
                        val filled = step < (progress / 25)
                        Box(
                            Modifier
                                .weight(1f)
                                .height(8.dp)
                                .padding(end = if (step < 3) 4.dp else 0.dp)
                                .background(
                                    if (filled) Theme.Gold else Theme.CardBorder,
                                    RoundedCornerShape(3.dp)
                                )
                        )
                    }
                    Spacer(Modifier.width(8.dp))
                    Text("$progress%", color = Theme.GoldBright, fontSize = 16.sp, fontWeight = FontWeight.Bold)
                }

                Spacer(Modifier.height(12.dp))
                Box(
                    Modifier
                        .fillMaxWidth()
                        .background(Theme.Card, RoundedCornerShape(8.dp))
                        .padding(12.dp)
                ) {
                    Column {
                        Text("门禁判定诊断：", color = Theme.Gold, fontSize = 12.sp, fontWeight = FontWeight.Bold)
                        Spacer(Modifier.height(2.dp))
                        Text(
                            if (gate.isBlank()) "指标正常，处于策略跟踪窗口中。" else gate,
                            color = Theme.TextPrimary,
                            fontSize = 13.sp
                        )
                    }
                }

                Spacer(Modifier.height(14.dp))
                Text("近 24 周期价格走势", color = Theme.TextMuted, fontSize = 12.sp)
                Spacer(Modifier.height(6.dp))
                SmoothSparkline(
                    values = wave,
                    modifier = Modifier
                        .fillMaxWidth()
                        .height(70.dp)
                        .clip(RoundedCornerShape(6.dp))
                        .background(Theme.Bg)
                        .border(1.dp, Theme.CardBorder, RoundedCornerShape(6.dp))
                        .padding(6.dp)
                )

                Spacer(Modifier.height(18.dp))
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    Box(
                        Modifier
                            .weight(1f)
                            .background(if (isWatchlist) Theme.GoldDim else Theme.Card, RoundedCornerShape(8.dp))
                            .border(1.dp, if (isWatchlist) Theme.Gold else Theme.CardBorder, RoundedCornerShape(8.dp))
                            .clickable(onClick = onToggleWatchlist)
                            .padding(vertical = 10.dp),
                        contentAlignment = Alignment.Center
                    ) {
                        Text(
                            if (isWatchlist) "★ 已设为关注" else "☆ 加入关注",
                            color = if (isWatchlist) Theme.GoldBright else Theme.TextSecondary,
                            fontSize = 13.sp,
                            fontWeight = FontWeight.Medium
                        )
                    }

                    Box(
                        Modifier
                            .weight(1f)
                            .background(Theme.Gold, RoundedCornerShape(8.dp))
                            .clickable { onCopy(symbol) }
                            .padding(vertical = 10.dp),
                        contentAlignment = Alignment.Center
                    ) {
                        Text("📋 复制代码", color = Theme.Bg, fontSize = 13.sp, fontWeight = FontWeight.Bold)
                    }
                }
            }
        }
    }
}

// ==========================================
// Status Page ("状态" Tab)
// ==========================================
@Composable
private fun StatusPage(
    state: JSONObject,
    currentBase: String,
    draft: String,
    lastPingMs: Long,
    onDraft: (String) -> Unit,
    onSave: () -> Unit,
    onSelectStrategy: (String) -> Unit
) {
    val server = state.optJSONObject("server")
    val feed = text(state, "feed")
    val alpaca = text(state, "alpaca")
    val calendar = text(state, "calendar")
    val uptimeSec = server?.optLong("uptime_seconds") ?: 0L
    val uptimeStr = formatUptime(uptimeSec)
    val pid = text(server, "pid")
    val worker = text(server, "worker")
    val bind = text(server, "bind")

    LazyColumn(
        Modifier
            .fillMaxSize()
            .padding(horizontal = 14.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        item {
            Spacer(Modifier.height(6.dp))
            Text("系统与节点状态", color = Theme.TextPrimary, fontSize = 20.sp, fontWeight = FontWeight.Bold)
            Text("Binance Stocks V2.2 模拟交易网络", color = Theme.TextMuted, fontSize = 12.sp)
        }

        item {
            Column(
                Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(12.dp))
                    .background(Theme.Surface)
                    .border(1.dp, Theme.CardBorder, RoundedCornerShape(12.dp))
                    .padding(14.dp)
            ) {
                Row(
                    Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text("核心进程", color = Theme.TextPrimary, fontSize = 15.sp, fontWeight = FontWeight.Bold)
                    Box(
                        Modifier
                            .background(Theme.MintDim, RoundedCornerShape(6.dp))
                            .padding(horizontal = 6.dp, vertical = 2.dp)
                    ) {
                        Text("PID $pid · 正常", color = Theme.Mint, fontSize = 11.sp, fontWeight = FontWeight.Bold)
                    }
                }

                Spacer(Modifier.height(8.dp))
                StatusRow("运行时长", uptimeStr)
                StatusRow("绑定网络", bind)
                StatusRow("行情线程", "$worker · 轮询监听")
                StatusRow("实盘安全锁", "🔒 物理关闭 (live_trade_enabled=False)")
                StatusRow("Alpaca 回放", alpaca)
                StatusRow("交易日历", calendar)
            }
        }

        item {
            Column(
                Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(12.dp))
                    .background(Theme.Surface)
                    .border(1.dp, Theme.CardBorder, RoundedCornerShape(12.dp))
                    .padding(14.dp)
            ) {
                Text("行情源监控 (Feed Ingestion)", color = Theme.TextPrimary, fontSize = 15.sp, fontWeight = FontWeight.Bold)
                Spacer(Modifier.height(8.dp))
                Box(
                    Modifier
                        .fillMaxWidth()
                        .background(Theme.Card, RoundedCornerShape(8.dp))
                        .padding(10.dp)
                ) {
                    Text(feed.ifEmpty { "行情流初始化中..." }, color = Theme.TextSecondary, fontSize = 12.sp, lineHeight = 18.sp)
                }
            }
        }

        item {
            Column(
                Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(12.dp))
                    .background(Theme.Surface)
                    .border(1.dp, Theme.CardBorder, RoundedCornerShape(12.dp))
                    .padding(14.dp)
            ) {
                Row(
                    Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text("切换服务节点", color = Theme.TextPrimary, fontSize = 15.sp, fontWeight = FontWeight.Bold)
                    if (lastPingMs >= 0) {
                        Text("延迟: ${lastPingMs}ms", color = Theme.Mint, fontSize = 11.sp, fontWeight = FontWeight.Bold)
                    }
                }

                Spacer(Modifier.height(8.dp))
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Box(
                        Modifier
                            .weight(1f)
                            .background(if (draft == DefaultBase) Theme.GoldDim else Theme.Card, RoundedCornerShape(6.dp))
                            .border(1.dp, if (draft == DefaultBase) Theme.Gold else Theme.CardBorder, RoundedCornerShape(6.dp))
                            .clickable { onDraft(DefaultBase) }
                            .padding(vertical = 8.dp),
                        contentAlignment = Alignment.Center
                    ) {
                        Text("🇯🇵 日本生产节点", color = if (draft == DefaultBase) Theme.GoldBright else Theme.TextSecondary, fontSize = 12.sp)
                    }

                    Box(
                        Modifier
                            .weight(1f)
                            .background(if (draft == LocalBase) Theme.GoldDim else Theme.Card, RoundedCornerShape(6.dp))
                            .border(1.dp, if (draft == LocalBase) Theme.Gold else Theme.CardBorder, RoundedCornerShape(6.dp))
                            .clickable { onDraft(LocalBase) }
                            .padding(vertical = 8.dp),
                        contentAlignment = Alignment.Center
                    ) {
                        Text("💻 本地开发节点", color = if (draft == LocalBase) Theme.GoldBright else Theme.TextSecondary, fontSize = 12.sp)
                    }
                }

                Spacer(Modifier.height(10.dp))
                BasicTextField(
                    value = draft,
                    onValueChange = onDraft,
                    singleLine = true,
                    textStyle = TextStyle(color = Theme.TextPrimary, fontSize = 14.sp, fontFamily = FontFamily.Monospace),
                    cursorBrush = SolidColor(Theme.Gold),
                    modifier = Modifier
                        .fillMaxWidth()
                        .background(Theme.Card, RoundedCornerShape(8.dp))
                        .border(1.dp, Theme.CardBorder, RoundedCornerShape(8.dp))
                        .padding(12.dp)
                )

                Spacer(Modifier.height(10.dp))
                Box(
                    Modifier
                        .fillMaxWidth()
                        .background(Theme.Gold, RoundedCornerShape(8.dp))
                        .clickable(onClick = onSave)
                        .padding(vertical = 11.dp),
                    contentAlignment = Alignment.Center
                ) {
                    Text("保存并立即连接", color = Theme.Bg, fontSize = 14.sp, fontWeight = FontWeight.Bold)
                }
            }
        }

        item {
            Text("策略运行总览 (点击可直达策略)", color = Theme.TextPrimary, fontSize = 16.sp, fontWeight = FontWeight.Bold)
        }

        val desks = state.optJSONArray("desks") ?: JSONArray()
        items(desks.length()) { index ->
            val desk = desks.getJSONObject(index)
            val strat = text(desk, "strategy")
            val title = text(desk, "title")
            val runtimeDesk = text(desk, "runtime")
            val reasonDesk = text(desk, "reason")
            val eq = text(desk, "equity")
            val posLen = count(desk, "positions")
            val workLen = count(desk, "working")
            val haltedDesk = desk.optBoolean("halt")

            Row(
                Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(10.dp))
                    .background(Theme.Surface)
                    .border(1.dp, Theme.CardBorder, RoundedCornerShape(10.dp))
                    .clickable { onSelectStrategy(strat) }
                    .padding(12.dp),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Column(Modifier.weight(1f)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Box(
                            Modifier
                                .background(Theme.GoldDim, RoundedCornerShape(4.dp))
                                .padding(horizontal = 6.dp, vertical = 2.dp)
                        ) {
                            Text(strat, color = Theme.Gold, fontSize = 12.sp, fontWeight = FontWeight.Bold)
                        }
                        Spacer(Modifier.width(6.dp))
                        Text(title, color = Theme.TextPrimary, fontSize = 14.sp, fontWeight = FontWeight.Bold)
                        if (haltedDesk) {
                            Spacer(Modifier.width(6.dp))
                            Text("● 停开", color = Theme.Coral, fontSize = 11.sp)
                        }
                    }
                    Spacer(Modifier.height(4.dp))
                    Text(
                        "$runtimeDesk · $reasonDesk · 持仓 $posLen · 挂单 $workLen",
                        color = Theme.TextMuted,
                        fontSize = 11.sp,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis
                    )
                }
                Text("$$eq", color = Theme.TextPrimary, fontSize = 15.sp, fontWeight = FontWeight.Bold)
            }
        }

        item {
            Spacer(Modifier.height(16.dp))
        }
    }
}

@Composable
private fun StatusRow(label: String, value: String) {
    Row(
        Modifier
            .fillMaxWidth()
            .padding(vertical = 3.dp),
        horizontalArrangement = Arrangement.SpaceBetween
    ) {
        Text(label, color = Theme.TextMuted, fontSize = 12.sp)
        Text(value, color = Theme.TextSecondary, fontSize = 12.sp, fontWeight = FontWeight.Medium)
    }
}

// ==========================================
// Empty States & Helper Views
// ==========================================
@Composable
private fun EmptyStateCard(text: String) {
    Box(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(8.dp))
            .background(Theme.Surface)
            .border(1.dp, Theme.CardBorder, RoundedCornerShape(8.dp))
            .padding(vertical = 16.dp, horizontal = 14.dp),
        contentAlignment = Alignment.Center
    ) {
        Text(text, color = Theme.TextMuted, fontSize = 12.sp, textAlign = TextAlign.Center)
    }
}

@Composable
private fun SectionHeader(title: String, count: Int, badge: String) {
    Row(
        Modifier
            .fillMaxWidth()
            .padding(top = 10.dp, bottom = 4.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(title, color = Theme.TextPrimary, fontSize = 15.sp, fontWeight = FontWeight.Bold)
            Spacer(Modifier.width(6.dp))
            Text("($count)", color = Theme.TextMuted, fontSize = 13.sp)
        }
        Box(
            Modifier
                .background(Theme.Card, RoundedCornerShape(4.dp))
                .padding(horizontal = 6.dp, vertical = 2.dp)
        ) {
            Text(badge, color = Theme.Gold, fontSize = 11.sp, fontWeight = FontWeight.Medium)
        }
    }
}

@Composable
private fun DisconnectedView(
    base: String,
    failure: String?,
    connecting: Boolean,
    onSwitchToLocal: () -> Unit,
    onSwitchToJapan: () -> Unit
) {
    Column(
        Modifier
            .fillMaxSize()
            .padding(24.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Text("⚡", fontSize = 48.sp)
        Spacer(Modifier.height(16.dp))
        Text(
            if (connecting) "正在连接控制台" else "无法连接控制台后端",
            color = Theme.TextPrimary,
            fontSize = 20.sp,
            fontWeight = FontWeight.Bold
        )
        Spacer(Modifier.height(8.dp))
        Text(
            if (connecting) "日本服务器往返大约要数秒\n$base" else (failure ?: base),
            color = Theme.TextSecondary,
            fontSize = 13.sp,
            textAlign = TextAlign.Center
        )

        Spacer(Modifier.height(24.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            Box(
                Modifier
                    .background(Theme.Gold, RoundedCornerShape(8.dp))
                    .clickable(onClick = onSwitchToJapan)
                    .padding(horizontal = 14.dp, vertical = 8.dp)
            ) {
                Text("切到日本服务器", color = Theme.Bg, fontSize = 13.sp, fontWeight = FontWeight.Bold)
            }
            Box(
                Modifier
                    .background(Theme.SurfaceElevated, RoundedCornerShape(8.dp))
                    .border(1.dp, Theme.CardBorder, RoundedCornerShape(8.dp))
                    .clickable(onClick = onSwitchToLocal)
                    .padding(horizontal = 14.dp, vertical = 8.dp)
            ) {
                Text("切到本地 (127.0.0.1)", color = Theme.TextPrimary, fontSize = 13.sp)
            }
        }
    }
}

// ==========================================
// Network & Data Helpers
// ==========================================
private fun waveOf(row: JSONObject): List<Float> {
    val wave = row.optJSONArray("wave") ?: return emptyList()
    return buildList {
        for (index in 0 until wave.length()) {
            wave.optString(index).toFloatOrNull()?.let { add(it) }
        }
    }
}

private fun text(node: JSONObject?, key: String): String {
    if (node == null || node.isNull(key)) {
        return ""
    }
    return node.optString(key)
}

private fun count(node: JSONObject, key: String): Int = node.optJSONArray(key)?.length() ?: 0

private fun formatUptime(seconds: Long): String {
    if (seconds < 60) return "${seconds}秒"
    val m = seconds / 60
    val s = seconds % 60
    if (m < 60) return "${m}分 ${s}秒"
    val h = m / 60
    val remM = m % 60
    return "${h}小时 ${remM}分"
}
