package com.joshuarodriguez.meetingrecorder

import android.accessibilityservice.AccessibilityService
import android.content.Intent
import android.os.Build
import android.telephony.PhoneStateListener
import android.telephony.TelephonyCallback
import android.telephony.TelephonyManager
import android.view.accessibility.AccessibilityEvent
import androidx.core.content.ContextCompat

/**
 * Why this exists at all:
 *
 * On Android 10+ a sideloaded app using plain MediaRecorder only gets
 * its own mic during a call (your side). The third-party recorders that
 * *do* capture the far side on a stock, non-rooted Pixel (Talker ACR,
 * Cube ACR, …) all run an AccessibilityService — being a bound
 * accessibility service is the privileged context the OS treats
 * differently for in-call audio, and it's the post-Android-10
 * mechanism Google left for non-Play distribution. We were missing it
 * entirely; this is that piece.
 *
 * Responsibilities:
 *  1. Exist as an enabled AccessibilityService (the user grants it in
 *     Settings → Accessibility; on Android 13+ they also tap
 *     "Allow restricted settings" — the onboarding walks them through
 *     it).
 *  2. Watch the phone call state and auto start/stop RecordingService
 *     so a regular cellular call records hands-free with the far side
 *     (capture source ladder in RecordingService does the actual
 *     VOICE_CALL→…→MIC attempt and reports what the device allowed).
 *
 * We deliberately do NOT scrape screen content; onAccessibilityEvent is
 * a no-op. The service's *enabled* state is the thing that matters for
 * call audio, plus the telephony callback for auto record.
 */
class CallAccessibilityService : AccessibilityService() {

    @Volatile private var inCall = false
    private var legacyListener: PhoneStateListener? = null
    private var modernCallback: TelephonyCallback? = null

    override fun onServiceConnected() {
        super.onServiceConnected()
        val tm = getSystemService(TelephonyManager::class.java) ?: return
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                val cb = object : TelephonyCallback(),
                    TelephonyCallback.CallStateListener {
                    override fun onCallStateChanged(state: Int) =
                        handleState(state)
                }
                modernCallback = cb
                tm.registerTelephonyCallback(mainExecutor, cb)
            } else {
                @Suppress("DEPRECATION")
                val l = object : PhoneStateListener() {
                    @Deprecated("Deprecated in Java")
                    override fun onCallStateChanged(state: Int, n: String?) =
                        handleState(state)
                }
                legacyListener = l
                @Suppress("DEPRECATION")
                tm.listen(l, PhoneStateListener.LISTEN_CALL_STATE)
            }
        } catch (_: SecurityException) {
            // READ_PHONE_STATE not granted yet — auto-record stays off
            // until the user grants it in onboarding. Manual recording
            // in the app still works regardless.
        }
    }

    private fun handleState(state: Int) {
        when (state) {
            TelephonyManager.CALL_STATE_OFFHOOK -> {
                if (!inCall) {
                    inCall = true
                    if (RecordingPrefs.autoRecordCalls(this)) startCallRecording()
                }
            }
            TelephonyManager.CALL_STATE_IDLE -> {
                if (inCall) {
                    inCall = false
                    stopCallRecording()
                }
            }
            // RINGING: do nothing; wait for OFFHOOK (answered/placed).
        }
    }

    private fun startCallRecording() {
        if (RecordingState.recording) return
        val sessionId = "CALL" + System.currentTimeMillis().toString(16).uppercase()
        RecordingState.reset()
        RecordingState.arm()
        val i = Intent(this, RecordingService::class.java).apply {
            action = RecordingService.ACTION_START
            putExtra(RecordingService.EXTRA_SESSION_ID, sessionId)
            putExtra(RecordingService.EXTRA_CAPTURE_MODE, "auto")
        }
        ContextCompat.startForegroundService(this, i)
    }

    private fun stopCallRecording() {
        if (!RecordingState.recording) return
        val i = Intent(this, RecordingService::class.java).apply {
            action = RecordingService.ACTION_STOP
        }
        startService(i)
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) { /* no-op */ }

    override fun onInterrupt() { /* no-op */ }

    override fun onUnbind(intent: Intent?): Boolean {
        try {
            val tm = getSystemService(TelephonyManager::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                modernCallback?.let { tm?.unregisterTelephonyCallback(it) }
            } else {
                @Suppress("DEPRECATION")
                legacyListener?.let { tm?.listen(it, PhoneStateListener.LISTEN_NONE) }
            }
        } catch (_: Exception) {
        }
        return super.onUnbind(intent)
    }

    companion object {
        /** Fully-qualified service name used in the Settings.Secure
         *  enabled-services check (see RecorderPlugin). */
        const val ID = "com.joshuarodriguez.meetingrecorder/" +
            "com.joshuarodriguez.meetingrecorder.CallAccessibilityService"
    }
}
