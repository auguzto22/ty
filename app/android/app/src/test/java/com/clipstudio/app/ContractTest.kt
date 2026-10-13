package com.clipstudio.app

import com.clipstudio.app.data.ApiJson
import com.clipstudio.app.data.Clip
import com.clipstudio.app.data.ClipEdit
import com.clipstudio.app.data.Health
import com.clipstudio.app.data.Project
import kotlinx.serialization.builtins.ListSerializer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** As fixtures em src/test/resources foram capturadas da API real (backend + worker) com o vídeo de teste. */
class ContractTest {
    private fun fixture(name: String) = javaClass.classLoader!!.getResource(name)!!.readText()

    @Test
    fun parsesRealProject() {
        val p = ApiJson.decodeFromString(Project.serializer(), fixture("project.json"))
        assertTrue(p.isDone)
        assertEquals("COMPLETE", p.stage)
        assertTrue(p.duration > 90)
    }

    @Test
    fun parsesRealClips() {
        val clips = ApiJson.decodeFromString(ListSerializer(Clip.serializer()), fixture("clips.json"))
        assertEquals(3, clips.size)
        val c = clips.first()
        assertEquals(1, c.rank)
        assertNotNull(c.preview)
        assertTrue(c.preview!!.url.startsWith("/clips/${c.id}/file"))
        assertTrue(c.exportReady)
        assertTrue(c.qa.ok)
        assertEquals("#FFE135", c.captions.highlightColor)
    }

    @Test
    fun parsesHealth() {
        val h = ApiJson.decodeFromString(Health.serializer(), fixture("health.json"))
        assertTrue(h.ok)
    }

    @Test
    fun clipEditOmitsUnsetFields() {
        val json = ApiJson.encodeToString(ClipEdit.serializer(), ClipEdit(start = 1.5, wordOverrides = mapOf(3 to "Olá")))
        assertEquals("""{"start":1.5,"word_overrides":{"3":"Olá"}}""", json)
    }
}
