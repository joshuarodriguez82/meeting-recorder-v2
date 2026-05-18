package com.joshuarodriguez.meetingrecorder

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.MediaRecorder
import android.os.Build
import android.os.IBinder
import android.os.SystemClock
import androidx.core.app.NotificationCompat
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Foreground service that owns the [MediaRecorder]. Lives in a
 * foreground service with type=microphone so Android keeps capturing
 * with the screen off / app backgrounded / phone in a pocket in the
 * car — the entire reason this is a native APK and not a PWA.
 *
 * Recording state is published on [RecordingState] (a process-global
 * holder) so [RecorderPlugin] can report status and collect the result
 * without binding/IPC.
 */
class RecordingService : Service() {

    private var recorder: MediaRecorder? = null
    private val running = AtomicBoolean(false)

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> start(
                intent.getStringExtra(EXTRA_SESSION_ID) ?: "REC",
                intent.getStringExtra(EXTRA_CAPTURE_MODE) ?: "auto",
            )
            ACTION_STOP -> stop()
            else -> stopSelf()
        }
        // Not sticky: if the OS kills us under memory pressure we do NOT
        // want a phantom restart with no recorder — the user would think
        // they're recording when they aren't.
        return START_NOT_STICKY
    }

    private fun newRecorder(): MediaRecorder =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) MediaRecorder(this)
        else @Suppress("DEPRECATION") MediaRecorder()

    /**
     * Ordered capture-source ladder.
     *
     * "auto" tries the call-audio sources first (these capture the FAR
     * end directly when the OS/OEM allows it — privileged on a stock
     * Pixel, available on some Samsung/older builds, and on rooted
     * devices via a Magisk module that exposes VOICE_CALL), then the
     * un-AEC'd mic sources (UNPROCESSED / VOICE_RECOGNITION cleanly
     * capture both sides on speakerphone), and finally plain MIC as the
     * guaranteed fallback. Whichever source actually prepares + starts
     * is reported on RecordingState.audioSource so the UI can show the
     * user exactly what their device permitted — no guessing.
     */
    private fun candidates(mode: String): List<Pair<Int, String>> {
        if (mode == "mic") {
            return listOf(MediaRecorder.AudioSource.MIC to "MIC")
        }
        return buildList {
            add(MediaRecorder.AudioSource.VOICE_CALL to "VOICE_CALL")
            add(MediaRecorder.AudioSource.VOICE_RECOGNITION to "VOICE_RECOGNITION")
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
                add(MediaRecorder.AudioSource.UNPROCESSED to "UNPROCESSED")
            }
            add(MediaRecorder.AudioSource.MIC to "MIC")
        }
    }

    private fun start(sessionId: String, captureMode: String) {
        if (running.get()) return
        val outFile = File(cacheDir, "rec_$sessionId.m4a")
        startForeground(NOTIF_ID, buildNotification())

        var lastErr: String? = null
        for ((source, label) in candidates(captureMode)) {
            val r = newRecorder()
            try {
                r.setAudioSource(source)
                r.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
                r.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
                r.setAudioChannels(1)
                r.setAudioSamplingRate(44100)
                r.setAudioEncodingBitRate(96000)
                r.setOutputFile(outFile.absolutePath)
                r.prepare()
                r.start()
                recorder = r
                running.set(true)
                RecordingState.onStarted(sessionId, outFile.absolutePath, label)
                return
            } catch (e: Exception) {
                // This source is denied on this device/OS (expected for
                // VOICE_CALL on a stock Pixel) — release and try the
                // next rung of the ladder.
                lastErr = "$label: ${e.message}"
                try { r.reset() } catch (_: Exception) {}
                try { r.release() } catch (_: Exception) {}
            }
        }
        RecordingState.onError(lastErr ?: "no usable audio source on this device")
        safeRelease()
        stopForegroundCompat()
        stopSelf()
    }

    private fun stop() {
        if (!running.getAndSet(false)) {
            // Nothing was recording; still release the notification.
            stopForegroundCompat()
            stopSelf()
            RecordingState.onFinalized()
            return
        }
        try {
            recorder?.stop()
            RecordingState.onStopped()
        } catch (e: Exception) {
            // stop() throws if it ran for ~0s or the encoder hiccuped.
            // The partial file is usually still playable up to the cut.
            RecordingState.onError("stop: ${e.message}")
        } finally {
            safeRelease()
            stopForegroundCompat()
            stopSelf()
            RecordingState.onFinalized()
        }
    }

    private fun safeRelease() {
        try {
            recorder?.reset()
            recorder?.release()
        } catch (_: Exception) {
        }
        recorder = null
    }

    private fun stopForegroundCompat() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            stopForeground(STOP_FOREGROUND_REMOVE)
        } else {
            @Suppress("DEPRECATION")
            stopForeground(true)
        }
    }

    private fun startForeground(id: Int, n: Notification) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(id, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        } else {
            startForeground(id, n)
        }
    }

    private fun buildNotification(): Notification {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            nm.createNotificationChannel(
                NotificationChannel(
                    CHANNEL, "Recording",
                    NotificationManager.IMPORTANCE_LOW,
                ).apply { setShowBadge(false) },
            )
        }
        val launch = packageManager.getLaunchIntentForPackage(packageName)
        val pi = PendingIntent.getActivity(
            this, 0, launch ?: Intent(),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return NotificationCompat.Builder(this, CHANNEL)
            .setContentTitle("Meeting Recorder")
            .setContentText("Recording in progress — tap to return")
            .setSmallIcon(android.R.drawable.presence_audio_online)
            .setOngoing(true)
            .setContentIntent(pi)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()
    }

    companion object {
        const val ACTION_START = "com.joshuarodriguez.meetingrecorder.START"
        const val ACTION_STOP = "com.joshuarodriguez.meetingrecorder.STOP"
        const val EXTRA_SESSION_ID = "sessionId"
        const val EXTRA_CAPTURE_MODE = "captureMode"
        private const val CHANNEL = "mr_recording"
        private const val NOTIF_ID = 4711
    }
}

/**
 * Process-global recording state + a stop barrier. The plugin starts
 * the service then awaits [stopLatch] so stopRecording() can return the
 * finished file path synchronously to JS.
 */
object RecordingState {
    @Volatile var recording: Boolean = false
        private set
    @Volatile var sessionId: String? = null
        private set
    @Volatile var filePath: String? = null
        private set
    // Which MediaRecorder.AudioSource actually started capturing:
    // "VOICE_CALL" means the device gave us the real call stream (both
    // sides off-speaker); "MIC"/"UNPROCESSED"/"VOICE_RECOGNITION" means
    // acoustic capture (needs speakerphone for the far end).
    @Volatile var audioSource: String? = null
        private set
    @Volatile var startElapsed: Long = 0
        private set
    @Volatile var lastDurationMs: Long = 0
        private set
    @Volatile var lastError: String? = null

    @Volatile private var latch: CountDownLatch? = null

    private fun freezeDuration() {
        if (startElapsed != 0L) {
            lastDurationMs = SystemClock.elapsedRealtime() - startElapsed
        }
    }

    fun arm() {
        latch = CountDownLatch(1)
        lastError = null
    }

    fun onStarted(id: String, path: String, source: String) {
        sessionId = id
        filePath = path
        audioSource = source
        startElapsed = SystemClock.elapsedRealtime()
        recording = true
    }

    fun onStopped() {
        freezeDuration()
        recording = false
    }

    fun onError(msg: String) {
        freezeDuration()
        lastError = msg
        recording = false
    }

    fun onFinalized() {
        latch?.countDown()
    }

    fun elapsedMs(): Long =
        if (recording && startElapsed != 0L)
            SystemClock.elapsedRealtime() - startElapsed
        else 0

    /** Block (bounded) until the service confirms finalization. */
    fun awaitFinalized(timeoutMs: Long): Boolean =
        latch?.await(timeoutMs, java.util.concurrent.TimeUnit.MILLISECONDS) ?: true

    fun reset() {
        recording = false
        sessionId = null
        filePath = null
        audioSource = null
        startElapsed = 0
    }
}
