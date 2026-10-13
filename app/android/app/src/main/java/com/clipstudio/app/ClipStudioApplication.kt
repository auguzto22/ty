package com.clipstudio.app

import android.app.Application
import com.clipstudio.app.data.AppGraph
import com.clipstudio.app.work.Notifications

class ClipStudioApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        AppGraph.init(this)
        Notifications.ensureChannel(this)
    }
}
