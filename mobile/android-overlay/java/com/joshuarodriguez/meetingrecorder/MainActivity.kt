package com.joshuarodriguez.meetingrecorder

import android.os.Bundle
import com.getcapacitor.BridgeActivity

class MainActivity : BridgeActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        // App-local plugin: register before the bridge spins up so the
        // WebView's registerPlugin("Recorder") resolves to our Kotlin.
        registerPlugin(RecorderPlugin::class.java)
        super.onCreate(savedInstanceState)
    }
}
