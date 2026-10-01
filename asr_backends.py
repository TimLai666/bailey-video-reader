"""Narrow ASR adapter seam. No remote backend or network transmission exists.

Future backends must use explicit opt-in, an approved destination and secret-safe
configuration outside evidence manifests. Do not add endpoints by guessing.
"""
class LocalWhisperBackend:
    name = 'local-faster-whisper'
    remote = False

    def __init__(self, reader, model_path, language, threads):
        self.reader=reader;self.model_path=model_path;self.language=language;self.threads=threads

    def transcribe(self, wav_path, output_directory, duration):
        return self.reader.transcribe_audio(wav_path, output_directory, 0,
            self.model_path, self.language, 0, duration, self.threads)


def get_backend(name, reader, model_path, language, threads):
    if name != 'local':
        raise ValueError('Only local ASR is implemented; remote ASR requires an explicit, verified integration')
    return LocalWhisperBackend(reader, model_path, language, threads)
