package com.joshuarodriguez.meetingrecorder

import android.content.Context

/**
 * Tiny SharedPreferences wrapper shared by the JS layer (via
 * RecorderPlugin) and the AccessibilityService. The service runs with
 * no WebView, so the auto-record-calls choice has to live somewhere
 * both can read.
 */
object RecordingPrefs {
    private const val FILE = "mr_prefs"
    private const val KEY_AUTO_CALLS = "auto_record_calls"

    private fun prefs(ctx: Context) =
        ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    /** Default ON: if the user enabled the accessibility service at
     *  all, they want calls captured automatically. */
    fun autoRecordCalls(ctx: Context): Boolean =
        prefs(ctx).getBoolean(KEY_AUTO_CALLS, true)

    fun setAutoRecordCalls(ctx: Context, on: Boolean) {
        prefs(ctx).edit().putBoolean(KEY_AUTO_CALLS, on).apply()
    }
}
