package com.clipstudio.app

import com.clipstudio.app.data.ApiClient
import com.clipstudio.app.data.ApiException
import com.clipstudio.app.data.ByteArraySource
import com.clipstudio.app.data.ResumableUploader
import kotlinx.coroutines.test.runTest
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import okhttp3.mockwebserver.SocketPolicy
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import java.io.ByteArrayOutputStream

/** Servidor falso que implementa o protocolo de upload do backend (308 + Range, 201 no fim). */
class FakeUploadServer(private val total: Int) : Dispatcher() {
    val stored = ByteArrayOutputStream()
    val ranges = mutableListOf<String>()
    var failNextChunks = 0
    var dropNextChunkAfterPartial = false

    private fun range() = if (stored.size() > 0) "bytes=0-${stored.size() - 1}" else null

    private fun partial(): MockResponse = MockResponse().setResponseCode(308).apply { range()?.let { setHeader("Range", it) } }

    override fun dispatch(request: RecordedRequest): MockResponse {
        val cr = request.getHeader("Content-Range")!!
        ranges += cr
        val spec = cr.removePrefix("bytes ").substringBefore("/")
        if (spec == "*") return if (stored.size() == total) done() else partial()
        if (failNextChunks > 0) {
            failNextChunks--
            return MockResponse().setResponseCode(503).setBody("""{"detail":"ocupado"}""")
        }
        val start = spec.substringBefore("-").toInt()
        if (start != stored.size()) return partial()
        val body = request.body.readByteArray()
        if (dropNextChunkAfterPartial) { // simula conexão caindo: só metade chegou
            dropNextChunkAfterPartial = false
            stored.write(body, 0, body.size / 2)
            return MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AFTER_REQUEST)
        }
        stored.write(body)
        return if (stored.size() == total) done() else partial()
    }

    private fun done() = MockResponse().setResponseCode(201)
        .setBody("""{"project":{"id":"p1","status":"UPLOADED","duration":12.5}}""")
}

class ResumableUploaderTest {
    private lateinit var server: MockWebServer
    private val data = ByteArray(2_500_000) { (it * 31 % 251).toByte() }

    @Before fun setUp() { server = MockWebServer() }
    @After fun tearDown() { server.shutdown() }

    private fun api() = ApiClient(server.url("/").toString(), "tok")

    @Test
    fun uploadsInChunksAndReturnsProject() = runTest {
        val fake = FakeUploadServer(data.size)
        server.dispatcher = fake
        server.start()
        val progress = mutableListOf<Long>()
        val p = ResumableUploader(api(), chunkSize = 1_000_000).upload("/uploads/u1", ByteArraySource(data)) { s, _ -> progress += s }
        assertEquals("UPLOADED", p.status)
        assertArrayEquals(data, fake.stored.toByteArray())
        assertEquals("bytes */2500000", fake.ranges.first())
        assertTrue(fake.ranges.contains("bytes 1000000-1999999/2500000"))
        assertEquals(data.size.toLong(), progress.last())
        assertEquals("Bearer tok", server.takeRequest().getHeader("Authorization"))
    }

    @Test
    fun resumesFromServerOffsetAfterDroppedConnectionAndRetriesOn503() = runTest {
        val fake = FakeUploadServer(data.size)
        fake.failNextChunks = 2
        fake.dropNextChunkAfterPartial = true
        server.dispatcher = fake
        server.start()
        ResumableUploader(api(), chunkSize = 1_000_000, baseDelayMs = 1, maxDelayMs = 2)
            .upload("/uploads/u1", ByteArraySource(data))
        assertArrayEquals(data, fake.stored.toByteArray())
        // depois da queda, o cliente consultou o servidor e retomou exatamente de onde os bytes pararam
        assertTrue(fake.ranges.any { it.startsWith("bytes 500000-") })
    }

    @Test
    fun nonRetryableErrorStopsImmediately() = runTest {
        server.enqueue(MockResponse().setResponseCode(400).setBody("""{"detail":"tamanho total diferente do declarado"}"""))
        server.start()
        try {
            ResumableUploader(api(), baseDelayMs = 1).upload("/uploads/u1", ByteArraySource(data))
            fail("deveria falhar")
        } catch (e: ApiException) {
            assertEquals(400, e.code)
            assertEquals("tamanho total diferente do declarado", e.message)
        }
        assertEquals(1, server.requestCount)
    }
}
