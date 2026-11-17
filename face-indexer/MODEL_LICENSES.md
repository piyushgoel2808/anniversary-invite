# Model Provenance, Checksums & Licenses

This prototype uses two pretrained ONNX models from the official [OpenCV Zoo](https://github.com/opencv/opencv_zoo) repository, pinned to commit **`48b10fa505aeb51fbd127fc60a5c660040e823b7`** (2026-10-20).

Weights are **not committed** to this repository; they are downloaded locally into `face-indexer/models/` via `uv run face-indexer download-models` and strictly verified against the pinned SHA-256 checksums and exact byte sizes below before loading.

---

## Pinned Model Manifest

| Component | Model File | Model Version | Upstream Commit | Exact Size (bytes) | SHA-256 Checksum | License |
| :--- | :--- | :--- | :--- | ---: | :--- | :--- |
| **Face Detector** | `face_detection_yunet_2023mar.onnx` | `yunet_2023mar` | `48b10fa505aeb51fbd127fc60a5c660040e823b7` | `232589` | `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` | **MIT** |
| **Face Recogniser** | `face_recognition_sface_2021dec.onnx` | `sface_2021dec` | `48b10fa505aeb51fbd127fc60a5c660040e823b7` | `38696353` | `0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79` | **Apache-2.0** |

Combined model version identifier recorded in match records: `yunet_2023mar+sface_2021dec`.

---

## Upstream Sources

- **YuNet weights:** `https://media.githubusercontent.com/media/opencv/opencv_zoo/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/face_detection_yunet/face_detection_yunet_2023mar.onnx`
- **YuNet license:** `https://raw.githubusercontent.com/opencv/opencv_zoo/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/face_detection_yunet/LICENSE`
- **SFace weights:** `https://media.githubusercontent.com/media/opencv/opencv_zoo/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/face_recognition_sface/face_recognition_sface_2021dec.onnx`
- **SFace license:** `https://raw.githubusercontent.com/opencv/opencv_zoo/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/face_recognition_sface/LICENSE`

---

## Model-Specific License Notices

### 1. YuNet (`face_detection_yunet_2023mar.onnx`) — MIT License

```text
MIT License

Copyright (c) 2020 Shiqi Yu <shiqi.yu@gmail.com>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### 2. SFace (`face_recognition_sface_2021dec.onnx`) — Apache License 2.0

```text
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```
