# Embedding model provenance

- Original model: Google EmbeddingGemma (308M parameters), https://huggingface.co/google/embeddinggemma-300m
- Distributed conversion: https://huggingface.co/onnx-community/embeddinggemma-300m-ONNX
- Pinned revision: `5090578d9565bb06545b4552f76e6bc2c93e4a66`
- Conversion/modification notice: the upstream ONNX-community derivative converts
the original model to ONNX and quantizes its matrix weights to Q4. This plugin
uses its `onnx/model_q4.onnx` and external `model_q4.onnx_data` without modifying
those files. Exported `sentence_embedding` includes the trained embedding pipeline.
- `tokenizer.json` SHA256: `4dda02faaf32bc91031dc8c88457ac272b00c1016cc679757d1c441b248b9c47`
- ONNX graph SHA256: `ad1dfee81a70f7944b9b9d1cc6e48075b832881cf33fab2f2b248be78f3f0043`
- ONNX weights SHA256: `599962c3143b040de2dd05e5975be3e9091dd067cacc6a8f7186e3203bab9e02`
- The service checks all three hashes before loading. Raw768-dimensional output
is truncated and L2-normalized by the MCP client to the selected supported size.
- CPU runtime: ONNX Runtime1.30.0, Microsoft, MIT license; installed as a separate
Python dependency with its own package license. Other Python dependencies retain
their package licenses, included by their distributions.

The included Google documents were retrieved from their official URLs on2026-10-04.
The Q4 conversion remains subject to the Gemma Terms, not the plugin's MIT license.
