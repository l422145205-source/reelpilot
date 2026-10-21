# Third party attribution

ReelPilot application code is MIT licensed. Model weights and source footage are downloaded separately.

| Material | Author and source | License and treatment |
|---|---|---|
| Tears of Steel movie | (CC) Blender Foundation, https://mango.blender.org/ | CC BY 3.0, https://mango.blender.org/sharing/ . The project downloads the film for local processing. Any derived preview is an edited excerpt, with attribution here and beside the preview. |
| Sintel trailer | (CC) Blender Foundation, https://durian.blender.org/ | CC BY 3.0, https://durian.blender.org/about/ . Full source credit information remains available through the original film. The trailer is used locally and is not redistributed in this repository. |
| Qwen3 VL Embedding 2B | Qwen team, https://huggingface.co/Qwen/Qwen3-VL-Embedding-2B | Apache 2.0. Weights are excluded from Git. The local embedding wrapper follows the official model's last non-padding token pooling and chat format. No upstream model training is claimed. |
| Qwen3 0.6B | Qwen team, https://huggingface.co/Qwen/Qwen3-0.6B | Apache 2.0. Optional local query planner. Weights excluded. |
| faster whisper base.en | SYSTRAN, https://github.com/SYSTRAN/faster-whisper and https://huggingface.co/Systran/faster-whisper-base.en | faster-whisper is MIT. The underlying Whisper model is released under MIT. CPU int8 transcription is used. |
| LM Format Enforcer | https://github.com/noamgat/lm-format-enforcer | MIT. Used as a token-level JSON Schema constraint, followed by independent application validation. |
| FFmpeg through imageio ffmpeg | https://github.com/imageio/imageio-ffmpeg | imageio-ffmpeg is BSD. The platform FFmpeg executable is a separate distribution with its own build/license; it is obtained by the dependency and is not vendored into the repository. |
| Synthetic fixture | ReelPilot contributors | Original programmatic media, MIT. Fixture labels are authored, not predictions from an AI model. |

The `copyright.txt` inside the upstream Tears of Steel download directory specifically concerns separately distributed soundtrack files under CC BY ND 3.0. It is **not** the movie's general license. This project uses the movie's CC BY 3.0 sharing terms and does not distribute a separate soundtrack.

Original films, raw ASR files, model weights, local credentials, and local runtime logs are not part of the source publication. Generated outputs retain their source-media attribution obligations.
