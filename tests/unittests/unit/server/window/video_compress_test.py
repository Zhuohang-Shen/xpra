#!/usr/bin/env python3
# This file is part of Xpra.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import unittest
from queue import Queue
from threading import Event, Thread, get_ident
from types import SimpleNamespace
from unittest.mock import patch

from xpra.server.window import video_compress
from xpra.server.window.video_compress import WindowVideoSource


class PipelineElement:

    def __init__(self) -> None:
        self.clean_count = 0
        self.clean_threads: list[int] = []

    def clean(self) -> None:
        self.clean_count += 1
        self.clean_threads.append(get_ident())


class Converter(PipelineElement):

    def __init__(self) -> None:
        super().__init__()
        self.inputs: list[str] = []

    def convert(self, value: str) -> str:
        if self.clean_count:
            raise RuntimeError("converter is closed")
        self.inputs.append(value)
        return "NV12"


class Encoder(PipelineElement):

    def __init__(self) -> None:
        super().__init__()
        self.inputs: list[str] = []

    def compress(self, value: str) -> None:
        if self.clean_count:
            raise RuntimeError("encoder is closed")
        self.inputs.append(value)


class EncodeWorker:

    def __init__(self) -> None:
        self.queue: Queue = Queue()
        self.errors: list[BaseException] = []
        self.thread_id = 0
        self.started = Event()
        self.thread = Thread(target=self.run, name="test-encode", daemon=True)
        self.thread.start()
        if not self.started.wait(2):
            raise RuntimeError("encode worker did not start")

    def call(self, optional: bool, callback, *args) -> None:
        self.queue.put((optional, callback, args))

    def run(self) -> None:
        self.thread_id = get_ident()
        self.started.set()
        while True:
            item = self.queue.get()
            try:
                if item is None:
                    return
                _optional, callback, args = item
                callback(*args)
            except BaseException as e:
                self.errors.append(e)
            finally:
                self.queue.task_done()

    def drain(self) -> None:
        self.queue.join()
        if self.errors:
            raise self.errors[0]

    def close(self) -> None:
        self.queue.put(None)
        self.queue.join()
        self.thread.join(2)
        if self.thread.is_alive():
            raise RuntimeError("encode worker did not stop")


class VideoCompressTest(unittest.TestCase):

    @staticmethod
    def make_source():
        source = object.__new__(WindowVideoSource)
        source.wid = 7
        source._csc_encoder = None
        source._video_encoder = None
        source.video_encoder_timer = 0
        source.b_frame_flush_timer = 0
        source.b_frame_flush_data = ()
        source.video_stream_file = None
        source.queue_packet = lambda *_args: None
        return source

    def exercise_fifo_cleanup(self, publish_csc_before_request: bool) -> None:
        source = self.make_source()
        worker = EncodeWorker()
        source.call_in_encode_thread = worker.call
        converter = Converter()
        encoder = Encoder()
        next_converter = Converter()
        next_encoder = Encoder()
        pipeline_paused = Event()
        finish_pipeline = Event()
        frame_encoded = Event()

        def encode_frame() -> None:
            if publish_csc_before_request:
                source._csc_encoder = converter
            pipeline_paused.set()
            if not finish_pipeline.wait(2):
                raise RuntimeError("pipeline setup was not released")
            if not publish_csc_before_request:
                source._csc_encoder = converter
            source._video_encoder = encoder
            encoder.compress(source._csc_encoder.convert("RGBX"))
            frame_encoded.set()

        def install_next_pipeline() -> None:
            if source._csc_encoder is not None or source._video_encoder is not None:
                raise AssertionError("stale pipeline survived queued cleanup")
            source._csc_encoder = next_converter
            source._video_encoder = next_encoder

        worker.call(True, encode_frame)
        try:
            self.assertTrue(pipeline_paused.wait(2))
            source.video_context_clean()
            if publish_csc_before_request:
                self.assertIs(source._csc_encoder, converter)
            else:
                self.assertIsNone(source._csc_encoder)
            self.assertIsNone(source._video_encoder)
            worker.call(True, install_next_pipeline)
            finish_pipeline.set()
            worker.drain()

            self.assertTrue(frame_encoded.is_set())
            self.assertEqual(converter.inputs, ["RGBX"])
            self.assertEqual(encoder.inputs, ["NV12"])
            self.assertEqual(converter.clean_count, 1)
            self.assertEqual(encoder.clean_count, 1)
            self.assertEqual(converter.clean_threads, [worker.thread_id])
            self.assertEqual(encoder.clean_threads, [worker.thread_id])
            self.assertIs(source._csc_encoder, next_converter)
            self.assertIs(source._video_encoder, next_encoder)
            self.assertEqual(next_converter.clean_count, 0)
            self.assertEqual(next_encoder.clean_count, 0)

            source.video_context_clean()
            worker.drain()
            self.assertEqual(next_converter.clean_count, 1)
            self.assertEqual(next_encoder.clean_count, 1)
        finally:
            finish_pipeline.set()
            worker.close()

    def test_cleanup_is_fifo_ordered_with_pipeline_setup(self) -> None:
        for publish_csc_before_request in (False, True):
            with self.subTest(publish_csc_before_request=publish_csc_before_request):
                self.exercise_fifo_cleanup(publish_csc_before_request)

    def test_full_cleanup_preserves_pipeline_until_encode_cleanup(self) -> None:
        source = self.make_source()
        source.init_vars()
        source.av_sync_timer = 0
        source.encode_queue = []
        source.encode_queue_max_size = 10
        source._mmap = None
        source.statistics = SimpleNamespace(
            encoding_totals={},
            encoding_pending={},
        )
        batch_cleaned = []
        source.batch_config = SimpleNamespace(cleanup=lambda: batch_cleaned.append(True))
        source.encode_ended = lambda: None
        queued = []
        source.call_in_encode_thread = (
            lambda optional, callback, *args: queued.append((optional, callback, args))
        )
        converter = Converter()
        encoder = Encoder()
        source._csc_encoder = converter
        source._video_encoder = encoder
        source.video_encoder_timer = 101
        source.b_frame_flush_timer = 102
        source.b_frame_flush_data = (encoder, converter, 0, 0, 0, None)
        removed_sources = []
        fake_glib = SimpleNamespace(source_remove=removed_sources.append)

        with patch.object(video_compress, "GLib", fake_glib):
            source.cleanup()

        self.assertIs(source._csc_encoder, converter)
        self.assertIs(source._video_encoder, encoder)
        self.assertCountEqual(removed_sources, [101, 102])
        self.assertEqual(batch_cleaned, [True])
        self.assertGreaterEqual(len(queued), 3)
        self.assertTrue(all(not optional for optional, _callback, _args in queued))

        for _optional, callback, args in queued:
            callback(*args)

        self.assertIsNone(source._csc_encoder)
        self.assertIsNone(source._video_encoder)
        self.assertEqual(converter.clean_count, 1)
        self.assertEqual(encoder.clean_count, 1)

    def test_encoder_reinitialization_queues_pipeline_cleanup(self) -> None:
        source = self.make_source()
        source._encoders = {}
        source._mmap = True
        source.non_video_encodings = ()
        source.common_video_encodings = ()
        converter = Converter()
        encoder = Encoder()
        source._csc_encoder = converter
        source._video_encoder = encoder
        queued = []
        source.call_in_encode_thread = (
            lambda optional, callback, *args: queued.append((optional, callback, args))
        )

        with patch.object(video_compress.WindowSource, "do_init_encoders"), \
                patch.object(video_compress, "has_codec", return_value=False):
            source.do_init_encoders()

        self.assertIs(source._csc_encoder, converter)
        self.assertIs(source._video_encoder, encoder)
        self.assertEqual(len(queued), 1)
        optional, callback, args = queued.pop()
        self.assertFalse(optional)
        callback(*args)
        self.assertIsNone(source._csc_encoder)
        self.assertIsNone(source._video_encoder)
        self.assertEqual(converter.clean_count, 1)
        self.assertEqual(encoder.clean_count, 1)


if __name__ == "__main__":
    unittest.main()
