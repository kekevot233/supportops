"""P5b：Fake SDK + 已安装 SDK/MockTransport，禁止真实 socket 连接。"""

import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hello_agents import HashingEmbedder
from supportops import SupportOpsError
from supportops.answering import DemoQuoteLLM
from supportops.answering import AnswerEngine
from supportops.document_index import LocalDocumentIndex
from supportops.qa_evaluation import _build_entries, _documents_for_case
from supportops.provider_adapters import CallLimits, ChatSDKConfig, CompatibleChatClient, CompatibleEmbedder, EmbeddingSDKConfig, ProviderCallLedger
from supportops.provider_runtime import compatible_runtime
from supportops.qa_cases import load_answer_dataset
from supportops.qa_evaluation import evaluate_answers
from supportops.qa_runtime import MeteredLLM, RuntimeSpec
from supportops.seed import load_seed_corpus


def chat_response(text='{"status":"insufficient_evidence","passages":[]}', **changes):
    response={"choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":text}}],
              "usage":{"prompt_tokens":11,"completion_tokens":7,"total_tokens":18}}
    response.update(changes)
    return response


class FakeSDK:
    def __init__(self, *, response=None, dimension=384):
        self.options=[];self.chat_calls=[];self.embedding_calls=[];self.closed=0
        self.response=response;self.dimension=dimension
        self.chat=SimpleNamespace(completions=SimpleNamespace(create=self._chat))
        self.embeddings=SimpleNamespace(create=self._embedding)

    def with_options(self,**kwargs):
        self.options.append(kwargs)
        return self

    def close(self):
        self.closed+=1

    def _chat(self,**kwargs):
        self.chat_calls.append(deepcopy(kwargs))
        if isinstance(self.response,Exception):raise self.response
        if self.response is not None:return deepcopy(self.response)
        return chat_response(DemoQuoteLLM().invoke(kwargs["messages"]))

    def _embedding(self,**kwargs):
        self.embedding_calls.append(deepcopy(kwargs))
        if isinstance(self.response,Exception):raise self.response
        if self.response is not None:return deepcopy(self.response)
        embedder=HashingEmbedder(self.dimension)
        return {"data":[{"index":i,"embedding":embedder.embed(text)} for i,text in reversed(list(enumerate(kwargs["input"])))],
                "usage":{"prompt_tokens":13,"total_tokens":13}}


class OfflineFixture(unittest.TestCase):
    def setUp(self):
        self.no_socket=patch("socket.socket.connect",side_effect=AssertionError("real network forbidden"))
        self.no_socket.start();self.addCleanup(self.no_socket.stop)

    def assert_code(self,code,action):
        with self.assertRaises(SupportOpsError) as caught:action()
        self.assertEqual(caught.exception.code,code)
        self.assertNotIn("PRIVATE",str(caught.exception))


class TestChatSDKAdapter(OfflineFixture):
    def test_explicit_retry_timeout_and_modern_output_parameter(self):
        sdk=FakeSDK();client=CompatibleChatClient(sdk,ChatSDKConfig(model="fixture-chat"))
        result=client.complete([{"role":"user","content":"test"}],max_tokens=123,temperature=0.0)
        self.assertEqual(sdk.options,[{"max_retries":0,"timeout":30.0}])
        request=sdk.chat_calls[0]
        self.assertEqual(request["max_completion_tokens"],123);self.assertNotIn("max_tokens",request)
        self.assertEqual(request["temperature"],0.0);self.assertFalse(request["stream"]);self.assertEqual(request["n"],1)
        self.assertNotIn("response_format",request);self.assertEqual(result.usage.total_tokens,18)

    def test_legacy_parameter_and_json_mode_require_explicit_selection(self):
        sdk=FakeSDK();client=CompatibleChatClient(sdk,ChatSDKConfig(model="fixture-chat",token_parameter="max_tokens",json_mode=True))
        client.complete([{"role":"user","content":"JSON test"}],max_tokens=100)
        request=sdk.chat_calls[0]
        self.assertEqual(request["max_tokens"],100);self.assertNotIn("max_completion_tokens",request)
        self.assertEqual(request["response_format"],{"type":"json_object"});self.assertNotIn("temperature",request)

    def test_input_is_copied_and_unknown_tools_cannot_enter_request(self):
        sdk=FakeSDK();client=CompatibleChatClient(sdk,ChatSDKConfig(model="fixture-chat"))
        messages=[{"role":"user","content":"test"}]
        client.complete(messages,max_tokens=10);messages[0]["content"]="changed"
        self.assertEqual(sdk.chat_calls[0]["messages"][0]["content"],"test")
        for invalid in ([],[{"role":[],"content":"x"}],[{"role":"tool","content":"x"}],
                        [{"role":"user","content":"x","tool_calls":[]}],[{"role":"user","content":[]}]) :
            self.assert_code("INVALID_REQUEST",lambda:client.complete(invalid,max_tokens=10))
        self.assertEqual(len(sdk.chat_calls),1)

    def test_incomplete_tool_or_refusal_response_preserves_receipt_and_fails(self):
        for change in ({"finish_reason":"length"},{"finish_reason":"content_filter"},{"finish_reason":"tool_calls"},
                       {"message":{"role":"assistant","content":"{}","refusal":"cannot comply"}},
                       {"message":{"role":"assistant","content":"{}","tool_calls":[{"id":"call"}]}}):
            with self.subTest(change=change):
                response=chat_response();response["choices"][0].update(change)
                sdk=FakeSDK(response=response);meter=MeteredLLM(CompatibleChatClient(sdk,ChatSDKConfig(model="fixture-chat")))
                self.assert_code("MODEL_RESPONSE_INVALID",lambda:meter.invoke([{"role":"user","content":"test"}],max_tokens=10))
                self.assertEqual(meter.receipts[0].total_tokens,18)
                self.assertEqual(len(sdk.chat_calls),1)

    def test_invalid_choice_shape_index_role_or_content_is_not_accepted(self):
        invalid=[chat_response(choices=[]),chat_response(choices=[{},{}])]
        for change in ({"index":True},{"index":1},{"message":{"role":"user","content":"{}"}},
                       {"message":{"role":"assistant","content":None}},
                       {"message":{"role":"assistant","content":"x"*16001}}):
            response=chat_response();response["choices"][0].update(change);invalid.append(response)
        for response in invalid:
            client=CompatibleChatClient(FakeSDK(response=response),ChatSDKConfig(model="fixture-chat"))
            self.assertEqual(client.complete([{"role":"user","content":"test"}],max_tokens=10).error_code,"MODEL_RESPONSE_INVALID")

    def test_missing_usage_stays_unknown_but_malformed_usage_is_rejected(self):
        for usage in (None,{"prompt_tokens":True,"completion_tokens":1,"total_tokens":2},
                      {"prompt_tokens":1,"completion_tokens":1,"total_tokens":3}):
            client=CompatibleChatClient(FakeSDK(response=chat_response(usage=usage)),ChatSDKConfig(model="fixture-chat"))
            result=client.complete([{"role":"user","content":"test"}],max_tokens=10)
            self.assertIsNone(result.usage)
            self.assertEqual(result.error_code,None if usage is None else "MODEL_RESPONSE_INVALID")

    def test_sdk_errors_are_sanitized_and_not_retried(self):
        sdk=FakeSDK(response=RuntimeError("PRIVATE key and provider trace"))
        client=CompatibleChatClient(sdk,ChatSDKConfig(model="fixture-chat"))
        self.assert_code("MODEL_UNAVAILABLE",lambda:client.complete([{"role":"user","content":"test"}],max_tokens=10))
        self.assertEqual(len(sdk.chat_calls),1)
        snapshot=client.ledger.snapshot();self.assertIsNone(snapshot["chat"]["actual_usage"])
        self.assertNotIn("PRIVATE",json.dumps(snapshot))

    def test_object_response_contract(self):
        sdk=FakeSDK();sdk.chat.completions.create=lambda **kwargs:SimpleNamespace(
            choices=[SimpleNamespace(index=0,finish_reason="stop",message=SimpleNamespace(role="assistant",content="{}"))],
            usage=SimpleNamespace(prompt_tokens=2,completion_tokens=3,total_tokens=5))
        client=CompatibleChatClient(sdk,ChatSDKConfig(model="fixture-chat"))
        self.assertEqual(client.complete([{"role":"user","content":"test"}],max_tokens=10).usage.total_tokens,5)

    def test_owned_and_borrowed_clients_have_distinct_idempotent_cleanup(self):
        for owned in (False,True):
            sdk=FakeSDK();client=CompatibleChatClient(sdk,ChatSDKConfig(model="fixture-chat"),owns_client=owned)
            client.close();client.close();self.assertEqual(sdk.closed,int(owned))
            self.assert_code("MODEL_UNAVAILABLE",lambda:client.complete([{"role":"user","content":"test"}],max_tokens=10))


class TestEmbeddingSDKAdapter(OfflineFixture):
    def client(self,sdk=None,**config):
        return CompatibleEmbedder(sdk or FakeSDK(dimension=8),EmbeddingSDKConfig(model="fixture-embedding",dimension=8,**config))

    def test_batched_vectors_are_reordered_and_float_encoding_is_explicit(self):
        sdk=FakeSDK(dimension=8);client=self.client(sdk)
        texts=["开发环境","部署说明","索引指南"]
        self.assertEqual(client.embed_batch(texts),HashingEmbedder(8).embed_batch(texts))
        self.assertEqual(sdk.embedding_calls[0]["encoding_format"],"float")
        self.assertNotIn("dimensions",sdk.embedding_calls[0])
        self.assertEqual(client.ledger.snapshot()["embedding"]["actual_usage"]["output_tokens"],0)

    def test_dimensions_are_opt_in_and_return_dimension_always_checked(self):
        sdk=FakeSDK(dimension=8);self.client(sdk,request_dimensions=True).embed("test")
        self.assertEqual(sdk.embedding_calls[0]["dimensions"],8)
        client=self.client(FakeSDK(dimension=4))
        self.assert_code("INDEX_UNAVAILABLE",lambda:client.embed("test"))
        self.assertEqual(client.ledger.snapshot()["embedding"]["actual_usage"]["input_tokens"],13)

    def test_count_and_character_batch_limits(self):
        sdk=FakeSDK(dimension=8);client=self.client(sdk,batch_size=2,max_batch_chars=5)
        client.embed_batch(["aaa","bbb","cc","d"])
        self.assertEqual([row["input"] for row in sdk.embedding_calls],[["aaa"],["bbb","cc"],["d"]])

    def test_all_inputs_are_checked_before_first_request_and_empty_batch_is_free(self):
        sdk=FakeSDK(dimension=8);client=self.client(sdk,max_text_chars=5)
        self.assertEqual(client.embed_batch([]),[])
        for values in (["valid","x"*6],["valid", ""],"not a batch"):
            self.assert_code("INVALID_REQUEST",lambda:client.embed_batch(values))
        self.assertEqual(sdk.embedding_calls,[])

    def test_invalid_indexes_counts_and_vectors_fail_without_partial_result(self):
        invalid=[[],[{"index":0,"embedding":[1.0]*8}]*2,
                 [{"index":True,"embedding":[1.0]*8},{"index":1,"embedding":[1.0]*8}],
                 [{"index":5,"embedding":[1.0]*8},{"index":1,"embedding":[1.0]*8}]]
        for vector in ([0.0]*8,[True]*8,[float("nan")]*8,[float("inf")]*8,"base64"):
            invalid.append([{"index":0,"embedding":vector},{"index":1,"embedding":[1.0]*8}])
        for data in invalid:
            sdk=FakeSDK(response={"data":data,"usage":{"prompt_tokens":2,"total_tokens":2}})
            client=self.client(sdk)
            self.assert_code("INDEX_UNAVAILABLE",lambda:client.embed_batch(["one","two"]))
            self.assertEqual(client.ledger.snapshot()["embedding"]["usage_receipt_count"],1)

    def test_later_batch_failure_retains_known_prior_usage_without_returning_vectors(self):
        sdk=FakeSDK(dimension=8);original=sdk.embeddings.create
        def create(**kwargs):
            if sdk.embedding_calls:raise RuntimeError("PRIVATE later batch")
            return original(**kwargs)
        sdk.embeddings.create=create;client=self.client(sdk,batch_size=1)
        self.assert_code("INDEX_UNAVAILABLE",lambda:client.embed_batch(["first","second"]))
        snapshot=client.ledger.snapshot()["embedding"]
        self.assertEqual(snapshot["request_attempts"],2);self.assertEqual(snapshot["usage_receipt_count"],1)
        self.assertIsNone(snapshot["actual_usage"]);self.assertEqual(snapshot["known_usage_subtotal"]["total_tokens"],13)

    def test_usage_is_not_inferred_and_sdk_failure_does_not_retry(self):
        sdk=FakeSDK(response=RuntimeError("PRIVATE embedding error"));client=self.client(sdk)
        self.assert_code("INDEX_UNAVAILABLE",lambda:client.embed("test"))
        self.assertEqual(len(sdk.embedding_calls),1)
        self.assertIsNone(client.ledger.snapshot()["embedding"]["actual_usage"])


class TestProviderBudgets(OfflineFixture):
    def test_chat_limit_is_enforced_before_sdk_create(self):
        sdk=FakeSDK();ledger=ProviderCallLedger(CallLimits(max_chat_requests=1))
        client=CompatibleChatClient(sdk,ChatSDKConfig(model="fixture-chat"),ledger=ledger)
        client.complete([{"role":"user","content":"test"}],max_tokens=10)
        self.assert_code("EVALUATION_LIMIT_EXCEEDED",lambda:client.complete([{"role":"user","content":"test"}],max_tokens=10))
        self.assertEqual(len(sdk.chat_calls),1);self.assertEqual(ledger.snapshot()["budget_rejections"],1)

    def test_shared_character_and_reserved_output_budgets(self):
        ledger=ProviderCallLedger(CallLimits(max_input_chars=5,max_reserved_output_tokens=10))
        ledger.begin("embedding",input_chars=3)
        self.assert_code("EVALUATION_LIMIT_EXCEEDED",lambda:ledger.begin("chat",input_chars=3,output_tokens=1))
        ledger.begin("chat",input_chars=2,output_tokens=10)
        self.assert_code("EVALUATION_LIMIT_EXCEEDED",lambda:ledger.begin("chat",input_chars=1,output_tokens=1))
        self.assertEqual(ledger.snapshot()["input_chars_reserved"],5)
        self.assertEqual(ledger.snapshot()["output_tokens_reserved"],10)

    def test_concurrent_admission_cannot_overrun_request_limit(self):
        ledger=ProviderCallLedger(CallLimits(max_chat_requests=3))
        def attempt(_):
            try:ledger.begin("chat",input_chars=1,output_tokens=1);return True
            except SupportOpsError:return False
        with ThreadPoolExecutor(max_workers=5) as pool:self.assertEqual(sum(pool.map(attempt,range(10))),3)
        self.assertEqual(ledger.snapshot()["chat"]["request_attempts"],3)


class TestCompatibleRuntime(OfflineFixture):
    @classmethod
    def setUpClass(cls):
        cls.corpus=load_seed_corpus();cls.dataset=load_answer_dataset(cls.corpus)

    def spec(self,**changes):
        return RuntimeSpec(runtime_id="sdk-fixture",execution="external",model_id="fixture-chat",model_revision="fixture-v1",
            embedding_id=changes.get("embedding_id","hashing-blake2b"),embedding_revision="v1")

    def subset(self,*ids):
        return self.dataset.model_copy(update={"cases":[case for case in self.dataset.cases if case.id in ids]})

    def test_equal_score_evidence_preserves_retrieval_order_not_packet_wall_clock(self):
        case=self.subset("RET-06").cases[0]
        index=LocalDocumentIndex();entries=_build_entries(index,_documents_for_case(self.corpus,case))
        result=index.query(case.question,entries,limit=5)
        result=result.model_copy(update={"hits":[hit.model_copy(update={"score":0.5}) for hit in result.hits]})
        for _ in range(10):
            engine=AnswerEngine(DemoQuoteLLM())
            generation=engine.generate(case.question,[],result,{})
            self.assertEqual(generation.draft.passages[0].label,"D1")
            self.assertEqual(engine.builder_config.recency_weight,0.0)

    def test_runtime_is_lazy_and_gate_precedes_sdk_factories(self):
        chat=Mock();embedding=Mock()
        runtime=compatible_runtime(self.spec(embedding_id="fixture-embedding"),ChatSDKConfig(model="fixture-chat"),
            chat_sdk_factory=chat,embedding=EmbeddingSDKConfig(model="fixture-embedding",dimension=8),embedding_sdk_factory=embedding)
        chat.assert_not_called();embedding.assert_not_called()
        self.assert_code("EXTERNAL_CALLS_DISABLED",lambda:evaluate_answers(self.corpus,self.subset("RET-02"),runtime=runtime))
        chat.assert_not_called();embedding.assert_not_called()

    def test_fake_sdk_uses_real_adapter_path_with_hashing_and_receipts(self):
        sdk=FakeSDK();runtime=compatible_runtime(self.spec(),ChatSDKConfig(model="fixture-chat"),chat_sdk_factory=lambda:sdk)
        report=evaluate_answers(self.corpus,self.subset("RET-02","REF-01"),runtime=runtime,allow_external_calls=True)
        self.assertEqual(report["cases"][0]["observed_status"],"answered")
        self.assertEqual(report["usage"]["actual_usage"]["total_tokens"],36)
        self.assertEqual(report["provider_calls"]["chat"]["request_attempts"],2)
        self.assertIsNone(report["pipeline"]["embedding_actual_usage"])
        self.assertEqual(sdk.closed,1);self.assertIsNone(report["provider_calls"]["cost"])

    def test_fake_sdk_full_embedding_path_reports_usage_and_closes_both_roots(self):
        chat=FakeSDK();embedding=FakeSDK(dimension=384)
        runtime=compatible_runtime(self.spec(embedding_id="fixture-embedding"),ChatSDKConfig(model="fixture-chat"),chat_sdk_factory=lambda:chat,
            embedding=EmbeddingSDKConfig(model="fixture-embedding",dimension=384),embedding_sdk_factory=lambda:embedding)
        report=evaluate_answers(self.corpus,self.subset("RET-02"),runtime=runtime,allow_external_calls=True)
        self.assertTrue(report["summary"]["quality_gate_passed"])
        self.assertGreater(report["provider_calls"]["embedding"]["request_attempts"],1)
        self.assertIsNotNone(report["pipeline"]["embedding_actual_usage"])
        self.assertEqual((chat.closed,embedding.closed),(1,1))

    def test_truncation_is_protocol_failure_not_refusal_and_keeps_usage(self):
        response=chat_response();response["choices"][0]["finish_reason"]="length"
        sdk=FakeSDK(response=response)
        report=evaluate_answers(self.corpus,self.subset("REF-01"),runtime=compatible_runtime(self.spec(),ChatSDKConfig(model="fixture-chat"),chat_sdk_factory=lambda:sdk),allow_external_calls=True)
        self.assertEqual(report["cases"][0]["error_code"],"MODEL_RESPONSE_INVALID")
        self.assertIsNone(report["cases"][0]["observed_status"])
        self.assertEqual(report["usage"]["actual_usage"]["total_tokens"],18)

    def test_quota_rejections_are_distinct_and_do_not_make_additional_requests(self):
        sdk=FakeSDK();runtime=compatible_runtime(self.spec(),ChatSDKConfig(model="fixture-chat"),chat_sdk_factory=lambda:sdk,limits=CallLimits(max_chat_requests=1))
        report=evaluate_answers(self.corpus,self.subset("RET-02","REF-01"),runtime=runtime,allow_external_calls=True)
        self.assertEqual(report["cases"][1]["error_code"],"EVALUATION_LIMIT_EXCEEDED")
        self.assertEqual(len(sdk.chat_calls),1)
        self.assertEqual(report["provider_calls"]["budget_rejections"],1)
        self.assertIsNone(report["usage"]["actual_usage"])

    def test_client_factory_failure_closes_previously_created_embedding(self):
        embedding=FakeSDK(dimension=8)
        runtime=compatible_runtime(self.spec(embedding_id="fixture-embedding"),ChatSDKConfig(model="fixture-chat"),
            chat_sdk_factory=Mock(side_effect=RuntimeError("PRIVATE setup")),
            embedding=EmbeddingSDKConfig(model="fixture-embedding",dimension=8),embedding_sdk_factory=lambda:embedding)
        self.assert_code("EVALUATION_SETUP_FAILED",lambda:runtime.open(allow_external_calls=True))
        self.assertEqual(embedding.closed,1)

    def test_configs_cannot_hide_external_execution_or_mismatched_models(self):
        for spec in (self.spec().model_copy(update={"execution":"offline_local"}),self.spec().model_copy(update={"model_id":"other"})):
            with self.assertRaises(ValueError):compatible_runtime(spec,ChatSDKConfig(model="fixture-chat"),chat_sdk_factory=FakeSDK)
        with self.assertRaises(ValueError):ChatSDKConfig(model="fixture-chat",api_key="not a field")

    def test_each_runtime_has_one_run_and_does_not_accumulate_reports(self):
        factory=Mock(side_effect=FakeSDK)
        runtime=compatible_runtime(self.spec(),ChatSDKConfig(model="fixture-chat"),chat_sdk_factory=factory)
        evaluate_answers(self.corpus,self.subset("RET-02"),runtime=runtime,allow_external_calls=True)
        self.assert_code("EVALUATION_SETUP_FAILED",lambda:runtime.open(allow_external_calls=True))
        self.assertEqual(factory.call_count,1)


class TestInstalledSDKMockTransport(OfflineFixture):
    def make_sdk(self,handler):
        import openai
        try:
            import httpx2 as transport_lib
        except ImportError:
            import httpx as transport_lib
        # 只替换 SDK 模块的 os 引用，不遍历、读取或修改真实 os.environ。
        self.environment_patch=patch("openai._client.os",SimpleNamespace(environ={}))
        self.environment_patch.start();self.addCleanup(self.environment_patch.stop)
        transport=transport_lib.Client(transport=transport_lib.MockTransport(handler),trust_env=False,follow_redirects=False)
        kwargs={"api_key":"fixture-key-not-real","base_url":"https://sdk-fixture.invalid/v1","organization":"","project":"",
                "max_retries":7,"http_client":transport}
        import inspect
        parameters=inspect.signature(openai.OpenAI).parameters
        for key in ("admin_api_key","webhook_secret"):
            if key in parameters:kwargs[key]=""
        sdk=openai.OpenAI(**kwargs)
        self.addCleanup(sdk.close)
        return sdk,transport_lib

    def test_real_sdk_serializes_chat_without_network(self):
        requests=[]
        def handler(request):
            requests.append(request)
            return lib.Response(200,json={"id":"fixture","object":"chat.completion","created":1,"model":"fixture-chat",**chat_response()})
        sdk,lib=self.make_sdk(handler)
        result=CompatibleChatClient(sdk,ChatSDKConfig(model="fixture-chat")).complete([{"role":"user","content":"JSON test"}],max_tokens=40)
        self.assertEqual(result.usage.total_tokens,18);self.assertEqual(len(requests),1)
        body=json.loads(requests[0].content);self.assertEqual(body["max_completion_tokens"],40)
        self.assertEqual(requests[0].url.path,"/v1/chat/completions")

    def test_real_sdk_server_error_does_not_retry(self):
        requests=[]
        def handler(request):
            requests.append(request);return lib.Response(500,json={"error":{"message":"PRIVATE mocked failure","type":"server_error"}})
        sdk,lib=self.make_sdk(handler)
        client=CompatibleChatClient(sdk,ChatSDKConfig(model="fixture-chat"))
        self.assert_code("MODEL_UNAVAILABLE",lambda:client.complete([{"role":"user","content":"test"}],max_tokens=10))
        self.assertEqual(len(requests),1)

    def test_real_sdk_embedding_float_response_is_not_implicitly_base64(self):
        requests=[]
        def handler(request):
            requests.append(request)
            return lib.Response(200,json={"object":"list","model":"fixture-embedding","data":[
                {"index":1,"object":"embedding","embedding":[0.0,1.0]},
                {"index":0,"object":"embedding","embedding":[1.0,0.0]}],"usage":{"prompt_tokens":2,"total_tokens":2}})
        sdk,lib=self.make_sdk(handler)
        client=CompatibleEmbedder(sdk,EmbeddingSDKConfig(model="fixture-embedding",dimension=2,request_dimensions=True))
        self.assertEqual(client.embed_batch(["first","second"]),[[1.0,0.0],[0.0,1.0]])
        self.assertEqual(json.loads(requests[0].content)["encoding_format"],"float")

    def test_real_sdk_timeout_is_single_attempt_and_sanitized(self):
        requests=[]
        def handler(request):
            requests.append(request);raise lib.ReadTimeout("PRIVATE mocked timeout",request=request)
        sdk,lib=self.make_sdk(handler)
        client=CompatibleChatClient(sdk,ChatSDKConfig(model="fixture-chat",timeout_seconds=1.0))
        self.assert_code("MODEL_UNAVAILABLE",lambda:client.complete([{"role":"user","content":"test"}],max_tokens=10))
        self.assertEqual(len(requests),1)


if __name__ == "__main__":
    unittest.main()
