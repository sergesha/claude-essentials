"""Service must reject truncation and serialize its one shared model."""
from pathlib import Path
import sys
import numpy as np
import pytest
sys.path.insert(0, str(Path(__file__).parents[1] / 'server'))

def test_service_validates_whole_batch_before_inference():
    import embedding_service as service
    class Tokenizer:
        def encode(self, text):
            return type('Encoding', (), {'ids':list(range(len(text)))})()
    class Session:
        def run(self, *args):raise AssertionError('oversized batch must fail before inference')
    runtime=service.Runtime.__new__(service.Runtime)
    runtime.tokenizer=Tokenizer();runtime.session=Session()
    with pytest.raises(ValueError,match='2048'):
        runtime.encode(['ok','x'*2049])

def test_service_returns_exported_sentence_embedding():
    import embedding_service as service
    class Tokenizer:
        def encode(self,text):return type('Encoding', (), {'ids':[1,2,3]})()
    class Session:
        def run(self,names,feed):
            assert names==['sentence_embedding']
            return [np.ones((1,768),dtype=np.float32)]
    runtime=service.Runtime.__new__(service.Runtime)
    runtime.tokenizer=Tokenizer();runtime.session=Session()
    result=np.asarray(runtime.encode(['text']))
    assert result.shape==(1,768) and np.isclose(np.linalg.norm(result),1)
