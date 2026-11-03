package com.bstocks.paperdesk

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.AudioAttributes
import android.media.RingtoneManager
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlin.coroutines.coroutineContext
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

internal data class DeskLink(
    val payload: JSONObject? = null,
    val failure: String? = null,
    val latencyMs: Long = -1L,
    val alert: String? = null,
)

internal object DeskBus {
    private val linkState = MutableStateFlow(DeskLink())
    val link: StateFlow<DeskLink> = linkState

    fun publish(next: DeskLink) {
        linkState.value = next
    }

    fun dismissAlert() {
        linkState.value = linkState.value.copy(alert = null)
    }
}

private const val WATCH_CHANNEL = "paperdesk.watch"
private const val SIGNAL_CHANNEL = "paperdesk.signals.ringtone"
private const val WATCH_NOTIFICATION = 7
private const val PREFS = "paperdesk"
private const val DefaultBase = "http://43.163.203.127:8765"

class DeskWatcherService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var job: Job? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannels()
        startInForeground("正在连接日本服务器")
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startInForeground("正在连接日本服务器")
        if (job?.isActive != true) {
            job = scope.launch { watch() }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    private suspend fun watch() {
        val prefs = getSharedPreferences(PREFS, MODE_PRIVATE)
        prefs.edit().putBoolean("watcher_on", true).apply()
        val seen = (prefs.getStringSet("seen_alert_ids", emptySet()) ?: emptySet()).toMutableSet()
        var primed = prefs.getBoolean("alerts_primed", false)
        var lastUptime = prefs.getLong("server_uptime", -1L)
        var lastPayload: JSONObject? = null
        while (coroutineContext.isActive) {
            val base = prefs.getString("base_japan", DefaultBase) ?: DefaultBase
            val started = System.currentTimeMillis()
            val result = fetchState(base)
            val latency = System.currentTimeMillis() - started
            if (result.isSuccess) {
                val body = result.getOrThrow()
                val uptime = body.optJSONObject("server")?.optLong("uptime_seconds") ?: 0L
                if (lastUptime >= 0 && uptime + 15 < lastUptime) {
                    seen.clear()
                    primed = false
                }
                lastUptime = uptime
                val fresh = freshAlert(body, seen, primed)
                primed = true
                trim(seen)
                prefs.edit()
                    .putStringSet("seen_alert_ids", seen.toSet())
                    .putBoolean("alerts_primed", true)
                    .putLong("server_uptime", uptime)
                    .apply()
                if (fresh != null) {
                    postSignal(fresh)
                }
                lastPayload = body
                val held = fresh ?: DeskBus.link.value.alert
                DeskBus.publish(DeskLink(body, null, latency, held))
                startInForeground("模拟盘监听中")
                delay(1000)
            } else {
                val message = result.exceptionOrNull()?.message ?: "无法连接后端"
                DeskBus.publish(DeskLink(lastPayload, message, -1L, DeskBus.link.value.alert))
                startInForeground(if (lastPayload == null) "正在重试日本服务器" else "连接中断，继续重试")
                delay(2000)
            }
        }
    }

    private fun startInForeground(text: String) {
        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notification = NotificationCompat.Builder(this, WATCH_CHANNEL)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle("V2.2 模拟盘")
            .setContentText(text)
            .setContentIntent(open)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .build()
        val type = if (Build.VERSION.SDK_INT >= 34) {
            ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE
        } else {
            0
        }
        ServiceCompat.startForeground(this, WATCH_NOTIFICATION, notification, type)
    }

    private fun postSignal(text: String) {
        val open = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP
            putExtra("alert", text)
        }
        val pending = PendingIntent.getActivity(
            this,
            1,
            open,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notification = NotificationCompat.Builder(this, SIGNAL_CHANNEL)
            .setSmallIcon(android.R.drawable.stat_notify_chat)
            .setContentTitle("策略条件达成")
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setCategory(NotificationCompat.CATEGORY_ALARM)
            .setPriority(NotificationCompat.PRIORITY_MAX)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setContentIntent(pending)
            .setFullScreenIntent(pending, true)
            .setAutoCancel(true)
            .build()
        val manager = getSystemService(NotificationManager::class.java)
        manager.notify((System.currentTimeMillis() % 100_000L).toInt() + 20, notification)
    }

    private fun createChannels() {
        val manager = getSystemService(NotificationManager::class.java)
        val watch = NotificationChannel(
            WATCH_CHANNEL,
            "模拟盘监听",
            NotificationManager.IMPORTANCE_LOW,
        ).apply {
            description = "保持与日本服务器的连接"
            setShowBadge(false)
        }
        val ringtone = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
        val signal = NotificationChannel(
            SIGNAL_CHANNEL,
            "策略达成铃声",
            NotificationManager.IMPORTANCE_HIGH,
        ).apply {
            description = "策略达成时弹窗并响铃"
            lockscreenVisibility = Notification.VISIBILITY_PUBLIC
            enableVibration(true)
            setSound(
                ringtone,
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                    .build(),
            )
        }
        manager.createNotificationChannel(watch)
        manager.createNotificationChannel(signal)
    }
}

class DeskWatchBoot : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        if (intent?.action != Intent.ACTION_BOOT_COMPLETED) {
            return
        }
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        if (!prefs.getBoolean("watcher_on", false)) {
            return
        }
        val launch = Intent(context, DeskWatcherService::class.java)
        try {
            androidx.core.content.ContextCompat.startForegroundService(context, launch)
        } catch (_: RuntimeException) {
            // A locked-down boot policy can refuse a foreground start. The next app open starts it.
        }
    }
}

internal fun fetchState(base: String): Result<JSONObject> = runCatching {
    val connection = (URL("$base/api/state").openConnection() as HttpURLConnection).apply {
        connectTimeout = 12_000
        readTimeout = 15_000
        requestMethod = "GET"
    }
    try {
        if (connection.responseCode != 200) {
            error("HTTP ${connection.responseCode}")
        }
        JSONObject(connection.inputStream.bufferedReader().readText())
    } finally {
        connection.disconnect()
    }
}

private fun freshAlert(state: JSONObject, seen: MutableSet<String>, primed: Boolean): String? {
    val alerts = state.optJSONArray("alerts") ?: return null
    var text: String? = null
    var extra = 0
    for (index in 0 until alerts.length()) {
        val item = alerts.getJSONObject(index)
        val id = item.optString("id")
        if (id.isEmpty() || !seen.add(id) || !primed) {
            continue
        }
        val kind = item.optString("kind")
        if (kind != "FILLED" && kind != "WORKING" && kind != "SIGNAL") {
            continue
        }
        val line = if (kind == "SIGNAL") {
            "${item.optString("strategy")} 信号达成 100%  ${item.optString("symbol")} 现价 ${item.optString("price")}"
        } else {
            "${item.optString("strategy")} ${item.optString("text")}  ${item.optString("side")} ${item.optString("symbol")} ${item.optString("qty")} @ ${item.optString("price")}"
        }
        if (text == null) {
            text = line
        } else {
            extra += 1
        }
    }
    return if (text == null) null else if (extra == 0) text else "$text，另有 $extra 条"
}

private fun trim(seen: MutableSet<String>) {
    if (seen.size <= 200) {
        return
    }
    val keep = seen.toList().takeLast(200)
    seen.clear()
    seen.addAll(keep)
}
