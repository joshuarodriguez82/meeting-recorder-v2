package com.joshuarodriguez.meetingrecorder

import android.Manifest
import android.content.ClipData
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.provider.Settings
import androidx.activity.result.ActivityResult
import androidx.core.content.ContextCompat
import androidx.documentfile.provider.DocumentFile
import com.getcapacitor.JSArray
import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.ActivityCallback
import com.getcapacitor.annotation.CapacitorPlugin
import com.getcapacitor.annotation.Permission
import java.io.File
import java.io.FileInputStream

@CapacitorPlugin(
    name = "Recorder",
    permissions = [
        Permission(strings = [Manifest.permission.RECORD_AUDIO], alias = "microphone"),
        Permission(strings = [Manifest.permission.POST_NOTIFICATIONS], alias = "notifications"),
        Permission(strings = [Manifest.permission.READ_PHONE_STATE], alias = "phone"),
    ],
)
class RecorderPlugin : Plugin() {

    // checkPermissions() / requestPermissions() are inherited from Plugin
    // and return { microphone, notifications } keyed by the aliases above
    // — exactly the shape the TS interface expects.

    // --- SAF folder grant --------------------------------------------

    @PluginMethod
    fun pickFolder(call: PluginCall) {
        val intent = Intent(Intent.ACTION_OPEN_DOCUMENT_TREE).apply {
            addFlags(
                Intent.FLAG_GRANT_READ_URI_PERMISSION or
                    Intent.FLAG_GRANT_WRITE_URI_PERMISSION or
                    Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION,
            )
        }
        startActivityForResult(call, intent, "folderPicked")
    }

    @ActivityCallback
    private fun folderPicked(call: PluginCall, result: ActivityResult) {
        val uri = result.data?.data
        if (result.resultCode != android.app.Activity.RESULT_OK || uri == null) {
            call.reject("cancelled")
            return
        }
        try {
            context.contentResolver.takePersistableUriPermission(
                uri,
                Intent.FLAG_GRANT_READ_URI_PERMISSION or
                    Intent.FLAG_GRANT_WRITE_URI_PERMISSION,
            )
        } catch (e: SecurityException) {
            call.reject("could not persist folder permission: ${e.message}")
            return
        }
        val label = DocumentFile.fromTreeUri(context, uri)?.name
            ?: uri.lastPathSegment ?: "Selected folder"
        call.resolve(JSObject().put("treeUri", uri.toString()).put("label", label))
    }

    @PluginMethod
    fun hasFolderAccess(call: PluginCall) {
        val target = call.getString("treeUri")
        if (target == null) {
            call.reject("treeUri required")
            return
        }
        val ok = context.contentResolver.persistedUriPermissions.any {
            it.uri.toString() == target && it.isWritePermission
        }
        call.resolve(JSObject().put("ok", ok))
    }

    // --- recording ----------------------------------------------------

    @PluginMethod
    fun startRecording(call: PluginCall) {
        val sessionId = call.getString("sessionId") ?: "REC"
        // "auto" = try the call-audio ladder then fall back; "mic" =
        // force plain mic (speakerphone). Default auto.
        val captureMode = call.getString("captureMode") ?: "auto"
        RecordingState.reset()
        RecordingState.arm()
        val intent = Intent(context, RecordingService::class.java).apply {
            action = RecordingService.ACTION_START
            putExtra(RecordingService.EXTRA_SESSION_ID, sessionId)
            putExtra(RecordingService.EXTRA_CAPTURE_MODE, captureMode)
        }
        ContextCompat.startForegroundService(context, intent)
        // Brief wait so a hard failure (permission yanked, mic busy)
        // surfaces as a rejected promise instead of a fake live timer.
        val deadline = System.currentTimeMillis() + 1800
        while (System.currentTimeMillis() < deadline) {
            if (RecordingState.recording) {
                call.resolve()
                return
            }
            RecordingState.lastError?.let {
                call.reject(it)
                return
            }
            Thread.sleep(40)
        }
        // Service is slow but not errored — let it run; getStatus will
        // reflect reality and the notification confirms to the user.
        call.resolve()
    }

    @PluginMethod
    fun stopRecording(call: PluginCall) {
        val intent = Intent(context, RecordingService::class.java).apply {
            action = RecordingService.ACTION_STOP
        }
        context.startService(intent)
        if (!RecordingState.awaitFinalized(6000)) {
            call.reject("recorder did not finalize in time")
            return
        }
        val path = RecordingState.filePath
        val err = RecordingState.lastError
        if (path == null || !File(path).exists()) {
            call.reject(err ?: "no audio file produced")
            return
        }
        call.resolve(
            JSObject()
                .put("path", path)
                .put("durationMs", RecordingState.lastDurationMs)
                .put("audioSource", RecordingState.audioSource),
        )
    }

    @PluginMethod
    fun getStatus(call: PluginCall) {
        call.resolve(
            JSObject()
                .put("recording", RecordingState.recording)
                .put("sessionId", RecordingState.sessionId)
                .put("audioSource", RecordingState.audioSource)
                .put("elapsedMs", RecordingState.elapsedMs()),
        )
    }

    // --- accessibility (call capture) --------------------------------

    @PluginMethod
    fun accessibilityStatus(call: PluginCall) {
        val enabled = try {
            val flat = Settings.Secure.getString(
                context.contentResolver,
                Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
            ) ?: ""
            flat.split(':').any {
                it.equals(CallAccessibilityService.ID, ignoreCase = true)
            }
        } catch (_: Exception) {
            false
        }
        call.resolve(
            JSObject()
                .put("enabled", enabled)
                .put("autoRecordCalls", RecordingPrefs.autoRecordCalls(context)),
        )
    }

    @PluginMethod
    fun openAccessibilitySettings(call: PluginCall) {
        try {
            val i = Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            context.startActivity(i)
            call.resolve()
        } catch (e: Exception) {
            call.reject(e.message ?: "could not open Accessibility settings")
        }
    }

    @PluginMethod
    fun setAutoRecordCalls(call: PluginCall) {
        RecordingPrefs.setAutoRecordCalls(
            context, call.getBoolean("enabled", true) == true,
        )
        call.resolve()
    }

    /** Auto-recorded calls finish while the WebView may be dead, so
     *  their .m4a sits in cache with no session JSON. The UI calls this
     *  on resume to find them and run them through the normal sync
     *  queue. */
    @PluginMethod
    fun pendingCaptures(call: PluginCall) {
        val arr = JSArray()
        try {
            cacheDir().listFiles()?.forEach { f ->
                val m = Regex("^rec_(.+)\\.m4a$").find(f.name)
                if (f.isFile && m != null && RecordingState.filePath != f.absolutePath) {
                    arr.put(
                        JSObject()
                            .put("sessionId", m.groupValues[1])
                            .put("path", f.absolutePath)
                            .put("sizeBytes", f.length()),
                    )
                }
            }
        } catch (_: Exception) {
        }
        call.resolve(JSObject().put("captures", arr))
    }

    private fun cacheDir(): File = context.cacheDir

    // --- SAF writes / reads ------------------------------------------

    @PluginMethod
    fun writeSession(call: PluginCall) {
        val treeUri = call.getString("treeUri")
        val baseName = call.getString("baseName")
        val json = call.getString("json")
        val audioPath = call.getString("audioPath")
        if (treeUri == null || baseName == null || json == null || audioPath == null) {
            call.reject("treeUri, baseName, json, audioPath required")
            return
        }
        // SAF tree IO is slow — never on the WebView thread.
        Thread {
            try {
                val dir = DocumentFile.fromTreeUri(context, Uri.parse(treeUri))
                    ?: throw IllegalStateException("folder not accessible")
                if (!dir.canWrite()) {
                    throw IllegalStateException(
                        "no write access — re-pick the OneDrive folder",
                    )
                }
                val audioUri = writeChild(
                    dir, "$baseName.m4a", "audio/mp4",
                ) { out -> FileInputStream(audioPath).use { it.copyTo(out) } }

                val jsonUri = writeChild(
                    dir, "$baseName.json", "application/json",
                ) { out -> out.write(json.toByteArray(Charsets.UTF_8)) }

                // Audio is safely in the synced folder — reclaim cache.
                runCatching { File(audioPath).delete() }
                call.resolve(
                    JSObject().put("audioUri", audioUri).put("jsonUri", jsonUri),
                )
            } catch (e: Exception) {
                call.reject(e.message ?: "write failed")
            }
        }.start()
    }

    /** Overwrites an existing same-named doc so a retry after a partial
     *  write self-heals instead of leaving "name (1).m4a" duplicates. */
    private fun writeChild(
        dir: DocumentFile,
        name: String,
        mime: String,
        body: (java.io.OutputStream) -> Unit,
    ): String {
        dir.findFile(name)?.delete()
        val doc = dir.createFile(mime, name)
            ?: throw IllegalStateException("could not create $name")
        context.contentResolver.openOutputStream(doc.uri)?.use { out ->
            body(out)
            out.flush()
        } ?: throw IllegalStateException("could not open $name for writing")
        return doc.uri.toString()
    }

    /** Hand the recording's .m4a + session JSON to whatever app the
     *  user picks (OneDrive). Android's OneDrive app has no writable
     *  folder a SAF picker can target, so this share-intent path is the
     *  supported way to land a phone recording in the same OneDrive
     *  folder the desktop watches. The .m4a is left in cache (NOT
     *  deleted like writeSession does) so a cancelled/retried share
     *  still has the audio. */
    @PluginMethod
    fun shareSession(call: PluginCall) {
        val audioPath = call.getString("audioPath")
        val json = call.getString("json")
        val baseName = call.getString("baseName")
        if (audioPath == null || json == null || baseName == null) {
            call.reject("audioPath, json, baseName required")
            return
        }
        try {
            val audioFile = File(audioPath)
            if (!audioFile.exists()) {
                call.reject("audio file missing — it may already be synced")
                return
            }
            val jsonFile = File(cacheDir(), "$baseName.json")
            jsonFile.writeText(json, Charsets.UTF_8)
            launchShare(listOf(audioFile, jsonFile))
            call.resolve()
        } catch (e: Exception) {
            call.reject(e.message ?: "share failed")
        }
    }

    /** Share a recording that's already been written to the synced
     *  folder (its cache .m4a was deleted by writeSession). Reads the
     *  folder's <baseName>.m4a + .json back into cache, then hands them
     *  to the share sheet — so "Send to OneDrive" works on every
     *  recording row, not only ones still queued. */
    @PluginMethod
    fun shareSyncedSession(call: PluginCall) {
        val treeUri = call.getString("treeUri")
        val baseName = call.getString("baseName")
        if (treeUri == null || baseName == null) {
            call.reject("treeUri, baseName required")
            return
        }
        Thread {
            try {
                val dir = DocumentFile.fromTreeUri(context, Uri.parse(treeUri))
                    ?: throw IllegalStateException("folder not accessible")
                val staged = ArrayList<File>(2)
                for (name in listOf("$baseName.m4a", "$baseName.json")) {
                    val doc = dir.findFile(name)
                        ?: throw IllegalStateException("$name not in synced folder")
                    val dest = File(cacheDir(), name)
                    context.contentResolver.openInputStream(doc.uri)?.use { ins ->
                        dest.outputStream().use { ins.copyTo(it) }
                    } ?: throw IllegalStateException("could not read $name")
                    staged.add(dest)
                }
                launchShare(staged)
                call.resolve()
            } catch (e: Exception) {
                call.reject(e.message ?: "share failed")
            }
        }.start()
    }

    private fun launchShare(files: List<File>) {
        val authority = "${context.packageName}.share"
        val uris = ArrayList<Uri>(files.size)
        for (f in files) {
            uris.add(
                androidx.core.content.FileProvider.getUriForFile(
                    context, authority, f, f.name,
                ),
            )
        }
        val send = Intent(Intent.ACTION_SEND_MULTIPLE).apply {
            type = "*/*"
            putParcelableArrayListExtra(Intent.EXTRA_STREAM, uris)
            // FLAG_GRANT_READ_URI_PERMISSION does NOT cover EXTRA_STREAM
            // URIs — only getData()/ClipData. Without ClipData the
            // target app (OneDrive) gets the URIs but can't read them
            // ("couldn't be uploaded"). Attaching them as ClipData makes
            // the OS grant read to whichever app the user picks.
            clipData = ClipData(
                "recording", arrayOf("*/*"), ClipData.Item(uris[0]),
            ).also { c ->
                for (i in 1 until uris.size) c.addItem(ClipData.Item(uris[i]))
            }
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        // Belt-and-suspenders: the chooser doesn't reliably propagate
        // the URI grant to the picked app (OneDrive kept rejecting with
        // "couldn't be uploaded"). Explicitly grant read to every app
        // that can handle the share so it works regardless of which the
        // user taps. Grants are dropped when the cache files go away.
        val targets = context.packageManager
            .queryIntentActivities(send, PackageManager.MATCH_DEFAULT_ONLY)
        for (ri in targets) {
            val pkg = ri.activityInfo.packageName
            for (u in uris) {
                context.grantUriPermission(
                    pkg, u, Intent.FLAG_GRANT_READ_URI_PERMISSION,
                )
            }
        }
        val chooser = Intent.createChooser(send, "Send recording to OneDrive")
            .addFlags(
                Intent.FLAG_ACTIVITY_NEW_TASK or
                    Intent.FLAG_GRANT_READ_URI_PERMISSION,
            )
        context.startActivity(chooser)
    }

    @PluginMethod
    fun listSyncedSessions(call: PluginCall) {
        val treeUri = call.getString("treeUri")
        if (treeUri == null) {
            call.reject("treeUri required")
            return
        }
        Thread {
            try {
                val dir = DocumentFile.fromTreeUri(context, Uri.parse(treeUri))
                    ?: throw IllegalStateException("folder not accessible")
                val re = Regex("^session_[0-9A-Fa-f]+\\.json$")
                val files = dir.listFiles()
                    .filter { it.isFile && re.matches(it.name ?: "") }
                    .sortedByDescending { it.lastModified() }
                    .take(300)
                val arr = JSArray()
                for (f in files) {
                    val text = context.contentResolver.openInputStream(f.uri)
                        ?.use { it.readBytes().toString(Charsets.UTF_8) } ?: continue
                    arr.put(
                        JSObject().put("name", f.name).put("json", text),
                    )
                }
                call.resolve(JSObject().put("sessions", arr))
            } catch (e: Exception) {
                call.reject(e.message ?: "list failed")
            }
        }.start()
    }

    @PluginMethod
    fun readTextFile(call: PluginCall) {
        val treeUri = call.getString("treeUri")
        val name = call.getString("name")
        if (treeUri == null || name == null) {
            call.reject("treeUri and name required")
            return
        }
        Thread {
            try {
                val dir = DocumentFile.fromTreeUri(context, Uri.parse(treeUri))
                val doc = dir?.findFile(name)
                val text = if (doc != null && doc.isFile) {
                    context.contentResolver.openInputStream(doc.uri)
                        ?.use { it.readBytes().toString(Charsets.UTF_8) }
                } else {
                    null
                }
                // Absent key reads back as null on the JS side
                // (content: string | null) — no null-overload juggling.
                val out = JSObject()
                if (text != null) out.put("content", text)
                call.resolve(out)
            } catch (e: Exception) {
                call.reject(e.message ?: "read failed")
            }
        }.start()
    }
}
