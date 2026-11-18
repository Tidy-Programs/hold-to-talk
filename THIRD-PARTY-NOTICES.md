# Third-party notices

This file is given for information only. It is not part of the Terms of Use
(`TERMS.md`).

Last checked: 2026-11-17.

Hold to talk itself comes under its own licence, the Hold to talk Licence
(see `LICENSE`), which does not cover anything below. It **does not contain or
redistribute** any of the software or models below. `install.bat` downloads
the libraries from the Python Package Index (PyPI) onto your PC, and the app
downloads the speech model from Hugging Face the first time it needs it (the
installer can do this for you). Each item stays under its own licence, which
you accept by installing it.

How this was checked on the date above: the licence of each library was read
from the metadata and licence files inside the installed package (the versions
in `requirements.lock.txt` and `requirements-cuda.lock.txt`, which are the
versions the installer installs), and the licences of the projects and models
were read from their official pages. Where a package says one thing and
something it contains says another, both are stated below.

## Libraries installed from `requirements.lock.txt` (every PC)

The table lists every package in the lock file. "Metadata" means the licence
the installed package itself declares.

| Package | Version | Licence (metadata) | Used for |
| --- | --- | --- | --- |
| faster-whisper | 1.2.1 | MIT | Speech recognition front end |
| ctranslate2 | 4.9.0 | MIT | Speech recognition engine |
| onnxruntime | 1.32.0 | MIT | Runs the voice-activity detector |
| tokenizers | 0.23.3 | Apache-2.0 (package classifier; the project's `LICENSE` is Apache License 2.0) | Text tokenizer for Whisper |
| huggingface-hub | 2.4.0 | Apache-2.0 | Downloads the speech model |
| av (PyAV) | 19.0.2 | BSD-3-Clause (the Python bindings; see the bundled FFmpeg libraries below) | Audio decoding |
| numpy | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | Audio arrays |
| sounddevice | 0.5.6 | MIT (bundles PortAudio, see below) | Microphone access |
| pystray | 0.19.5 | LGPL-3.0 (classifier "LGPLv3") | Tray icon. Installed as a separate, unmodified library from PyPI, so you can replace it with another version |
| pillow | 13.0.0 | MIT-CMU | Drawing the icons |
| six | 1.17.0 | MIT | Dependency of pystray |
| cffi | 2.1.1 | MIT-0 | Dependency of sounddevice |
| pycparser | 3.12 | BSD-3-Clause | Dependency of cffi |
| PyYAML | 6.0.3 | MIT | Dependency of ctranslate2 |
| tqdm | 4.70.1 | MPL-2.0 AND MIT | Download progress bars |
| colorama | 0.4.6 | BSD (classifier "BSD License"; the package ships its licence file) | Dependency of tqdm and click |
| click | 8.5.1 | BSD-3-Clause | Dependency of huggingface-hub |
| filelock | 4.2.10 | MIT | Dependency of huggingface-hub |
| fsspec | 2026.9.0 | BSD-3-Clause | Dependency of huggingface-hub |
| hf-xet | 1.8.0 | Apache-2.0 | Faster downloads from Hugging Face |
| httpx2, httpcore2 | 2.16.0 | BSD-3-Clause | HTTP for huggingface-hub |
| h11 | 0.16.0 | MIT | Dependency of httpcore2 |
| anyio | 4.16.0 | MIT | Dependency of httpx2 |
| idna | 3.20 | BSD-3-Clause | Dependency of httpx2 |
| truststore | 0.10.5 | MIT | Dependency of httpx2 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | Dependency of huggingface-hub |
| typing-extensions | 4.16.0 | PSF-2.0 | Dependency of huggingface-hub |
| protobuf | 7.36.2 | BSD-3-Clause ("3-Clause BSD License") | Dependency of onnxruntime |
| flatbuffers | 25.12.19 | Apache-2.0 | Dependency of onnxruntime |

The environment also contains `pip` (MIT; it is not in the lock file). Python
itself (PSF licence) and its Tcl/Tk (`tkinter`) are installed by you from
python.org; they are not part of this project. With `-UseRanges`, the
installer installs newer packages inside the ranges of `requirements.txt`,
which may have other licences.

### Components bundled inside those packages

- **Silero VAD** (the voice-activity model faster-whisper uses,
  `faster_whisper/assets/silero_vad_v6.onnx`): MIT License, "Copyright (c)
  2020-present Silero Team" (the `LICENSE` file of
  github.com/snakers4/silero-vad).
- **PortAudio** (microphone library): the sounddevice wheel bundles
  pre-built PortAudio DLLs in `_sounddevice_data\portaudio-binaries`. Their
  `README.md` states: PortAudio by Ross Bencina and Phil Burk, MIT License;
  the DLLs with `-asio` in their name additionally involve Steinberg's ASIO
  SDK. PortAudio's own site (portaudio.com) describes an MIT-style licence.
  The app uses the default DLL (`libportaudio64bit.dll`), not the `-asio` ones.
- **FFmpeg libraries** (inside `av.libs` in the av wheel): avcodec, avformat,
  avutil, avfilter, avdevice, swresample and swscale, plus codec and support
  libraries including libx264, libx265, libvpx, libopus, libdav1d,
  libSvtAv1Enc, libvpl, libvmaf, libwebp, libiconv and zlib. The wheel's
  metadata declares BSD-3-Clause for the PyAV Python code. The bundled
  libraries keep their own licences: FFmpeg is published under the GNU LGPL
  or, depending on how it is built, the GNU GPL, and the x264 and x265
  projects are published under the GNU GPL. They are installed from PyPI as
  part of the unmodified av package and are not changed or redistributed by
  this project. This app uses PyAV only because faster-whisper depends on
  it, and does not encode video. The licence texts are published by the
  FFmpeg, x264 and x265 projects.
- **ctranslate2** (4.9.0): MIT (its `LICENSE` at github.com/OpenNMT/CTranslate2:
  "MIT License", SYSTRAN and The OpenNMT Authors). Its wheel also contains
  `libiomp5md.dll` (Intel(R) OpenMP* Runtime Library, copyright Intel
  Corporation) and `cudnn64_9.dll` (NVIDIA cuDNN, copyright NVIDIA), which
  are under Intel's and NVIDIA's own licence terms.
- **onnxruntime** ships its own `ThirdPartyNotices.txt` inside the package
  (`onnxruntime\ThirdPartyNotices.txt`).
- **numpy** bundles OpenBLAS (`libscipy_openblas64_...dll`, BSD-3-Clause per
  numpy's licence expression) and the Microsoft Visual C++ runtime
  (`msvcp140...dll`).

## NVIDIA runtime (`requirements-cuda.lock.txt`, only on PCs with an NVIDIA card)

| Package | Version | Licence (metadata) |
| --- | --- | --- |
| nvidia-cublas-cu12 | 12.9.2.10 | `LicenseRef-NVIDIA-Proprietary` (the file `licenses/License.txt` inside the package) |
| nvidia-cuda-nvrtc-cu12 | 12.9.86 | `LicenseRef-NVIDIA-Proprietary` ("Other/Proprietary License"; the file `licenses/License.txt`) |
| nvidia-cudnn-cu12 | 9.28.1.4 | `LicenseRef-NVIDIA-Proprietary` (the file `licenses/LICENSE`) |

**By installing these packages you accept NVIDIA's licence for them.** They are
free to use but are not open source and must not be redistributed by this
project; they are downloaded from PyPI to your PC only. If you do not accept
the licence, do not install them: the app then runs speech recognition on the
processor. The full texts are inside the installed packages (the
`nvidia_*\licenses` folders in `%LOCALAPPDATA%\HoldToTalk\venv\Lib\site-packages`
under their `*.dist-info` folders).

## Models (downloaded on demand)

| Model | Where it comes from | Licence |
| --- | --- | --- |
| Whisper `small` (faster-whisper conversion) | Hugging Face `Systran/faster-whisper-small` | MIT (declared on the model card) |
| Whisper `large-v3-turbo` (faster-whisper conversion) | Hugging Face `mobiuslabsgmbh/faster-whisper-large-v3-turbo` | MIT (declared on the model card) |
| `gemma4:e2b-it-qat` (optional AI cleanup, about 4.3 GB) | Ollama library, `ollama pull gemma4:e2b-it-qat` | Apache License 2.0 (the licence file shown on the model's Ollama page opens with "Apache License Version 2.0, January 2004") |

The original Whisper models were released by OpenAI under the MIT licence
(the `LICENSE` of github.com/openai/whisper: "MIT License", Copyright (c) 2022
OpenAI). You can choose other models in the settings file (`model`,
`cpu_model`, `cleanup.model`); those carry their own licences.

## Programs the app uses but does not install

- **Ollama** (optional, you install it yourself): MIT ("Copyright (c) Ollama"
  in the `LICENSE` of github.com/ollama/ollama). The app talks to Ollama only on this computer; see the README's AI cleanup section.
- **Microsoft Edge** or **Google Chrome**: the window is shown by one of them
  in "app mode". Neither is part of this project or installed by it; they are
  under their own terms.
- **Windows**, **PowerShell**, **Python** (python.org).
