# What is this?
## File for 'response_cost' calculation in Logging
import time
from functools import lru_cache
from typing import Any, List, Literal, Optional, Tuple, Union, cast

from pydantic import BaseModel

import litellm
import litellm._logging
from litellm import verbose_logger
from litellm.constants import (
    DEFAULT_MAX_LRU_CACHE_SIZE,
    DEFAULT_REPLICATE_GPU_PRICE_PER_SECOND,
)
from litellm.litellm_core_utils.llm_cost_calc.tool_call_cost_tracking import (
    StandardBuiltInToolCostTracking,
)
from litellm.litellm_core_utils.llm_cost_calc.utils import (
    _generic_cost_per_character,
    generic_cost_per_token,
)
from litellm.llms.anthropic.cost_calculation import (
    cost_per_token as anthropic_cost_per_token,
)
from litellm.llms.azure.cost_calculation import (
    cost_per_token as azure_openai_cost_per_token,
)
from litellm.llms.bedrock.image.cost_calculator import (
    cost_calculator as bedrock_image_cost_calculator,
)
from litellm.llms.databricks.cost_calculator import (
    cost_per_token as databricks_cost_per_token,
)
from litellm.llms.deepseek.cost_calculator import (
    cost_per_token as deepseek_cost_per_token,
)
from litellm.llms.fireworks_ai.cost_calculator import (
    cost_per_token as fireworks_ai_cost_per_token,
)
from litellm.llms.gemini.cost_calculator import cost_per_token as gemini_cost_per_token
from litellm.llms.openai.cost_calculation import (
    cost_per_second as openai_cost_per_second,
)
from litellm.llms.openai.cost_calculation import cost_per_token as openai_cost_per_token
from litellm.llms.together_ai.cost_calculator import get_model_params_and_category
from litellm.llms.vertex_ai.cost_calculator import (
    cost_per_character as google_cost_per_character,
)
from litellm.llms.vertex_ai.cost_calculator import (
    cost_per_token as google_cost_per_token,
)
from litellm.llms.vertex_ai.cost_calculator import cost_router as google_cost_router
from litellm.llms.vertex_ai.image_generation.cost_calculator import (
    cost_calculator as vertex_ai_image_cost_calculator,
)
from litellm.responses.utils import ResponseAPILoggingUtils
from litellm.types.llms.openai import (
    HttpxBinaryResponseContent,
    OpenAIRealtimeStreamList,
    OpenAIRealtimeStreamResponseBaseObject,
    OpenAIRealtimeStreamSessionEvents,
    ResponseAPIUsage,
    ResponsesAPIResponse,
)
from litellm.types.rerank import RerankBilledUnits, RerankResponse
from litellm.types.utils import (
    CallTypesLiteral,
    LlmProviders,
    LlmProvidersSet,
    ModelInfo,
    PassthroughCallTypes,
    StandardBuiltInToolsParams,
    Usage,
)
from litellm.utils import (
    CallTypes,
    CostPerToken,
    EmbeddingResponse,
    ImageResponse,
    ModelResponse,
    ProviderConfigManager,
    TextCompletionResponse,
    TranscriptionResponse,
    _cached_get_model_info_helper,
    token_counter,
)


def _cost_per_token_custom_pricing_helper(
    prompt_tokens: float = 0,
    completion_tokens: float = 0,
    response_time_ms: Optional[float] = 0.0,
    ### CUSTOM PRICING ###
    custom_cost_per_token: Optional[CostPerToken] = None,
    custom_cost_per_second: Optional[float] = None,
) -> Optional[Tuple[float, float]]:
    """Internal helper function for calculating cost, if custom pricing given"""
    if custom_cost_per_token is None and custom_cost_per_second is None:
        return None

    if custom_cost_per_token is not None:
        input_cost = custom_cost_per_token["input_cost_per_token"] * prompt_tokens
        output_cost = custom_cost_per_token["output_cost_per_token"] * completion_tokens
        return input_cost, output_cost
    elif custom_cost_per_second is not None:
        output_cost = custom_cost_per_second * response_time_ms / 1000  # type: ignore
        return 0, output_cost

    return None


def cost_per_token(  # noqa: PLR0915
    model: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    response_time_ms: Optional[float] = 0.0,
    custom_llm_provider: Optional[str] = None,
    region_name=None,
    ### CHARACTER PRICING ###
    prompt_characters: Optional[int] = None,
    completion_characters: Optional[int] = None,
    ### PROMPT CACHING PRICING ### - used for anthropic
    cache_creation_input_tokens: Optional[int] = 0,
    cache_read_input_tokens: Optional[int] = 0,
    ### CUSTOM PRICING ###
    custom_cost_per_token: Optional[CostPerToken] = None,
    custom_cost_per_second: Optional[float] = None,
    ### NUMBER OF QUERIES ###
    number_of_queries: Optional[int] = None,
    ### USAGE OBJECT ###
    usage_object: Optional[Usage] = None,  # just read the usage object if provided
    ### BILLED UNITS ###
    rerank_billed_units: Optional[RerankBilledUnits] = None,
    ### CALL TYPE ###
    call_type: CallTypesLiteral = "completion",
    audio_transcription_file_duration: float = 0.0,  # for audio transcription calls - the file time in seconds
) -> Tuple[float, float]:  # type: ignore
    """
    Calculates the cost per token for a given model, prompt tokens, and completion tokens.

    Parameters:
        model (str): The name of the model to use. Default is ""
        prompt_tokens (int): The number of tokens in the prompt.
        completion_tokens (int): The number of tokens in the completion.
        response_time (float): The amount of time, in milliseconds, it took the call to complete.
        prompt_characters (float): The number of characters in the prompt. Used for vertex ai cost calculation.
        completion_characters (float): The number of characters in the completion response. Used for vertex ai cost calculation.
        custom_llm_provider (str): The llm provider to whom the call was made (see init.py for full list)
        custom_cost_per_token: Optional[CostPerToken]: the cost per input + output token for the llm api call.
        custom_cost_per_second: Optional[float]: the cost per second for the llm api call.
        call_type: Optional[str]: the call type

    Returns:
        tuple: A tuple containing the cost in USD dollars for prompt tokens and completion tokens, respectively.
    """
    if model is None:
        raise Exception("Invalid arg. Model cannot be none.")

    ## RECONSTRUCT USAGE BLOCK ##
    if usage_object is not None:
        usage_block = usage_object
    else:
        usage_block = Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
        )

    ## CUSTOM PRICING ##
    response_cost = _cost_per_token_custom_pricing_helper(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        response_time_ms=response_time_ms,
        custom_cost_per_second=custom_cost_per_second,
        custom_cost_per_token=custom_cost_per_token,
    )

    if response_cost is not None:
        return response_cost[0], response_cost[1]

    # given
    prompt_tokens_cost_usd_dollar: float = 0
    completion_tokens_cost_usd_dollar: float = 0
    model_cost_ref = litellm.model_cost
    model_with_provider = model
    if custom_llm_provider is not None:
        model_with_provider = custom_llm_provider + "/" + model
        if region_name is not None:
            model_with_provider_and_region = (
                f"{custom_llm_provider}/{region_name}/{model}"
            )
            if (
                model_with_provider_and_region in model_cost_ref
            ):  # use region based pricing, if it's available
                model_with_provider = model_with_provider_and_region
    else:
        _, custom_llm_provider, _, _ = litellm.get_llm_provider(model=model)
    model_without_prefix = model
    model_parts = model.split("/", 1)
    if len(model_parts) > 1:
        model_without_prefix = model_parts[1]
    else:
        model_without_prefix = model
    """
    Code block that formats model to lookup in litellm.model_cost
    Option1. model = "bedrock/ap-northeast-1/anthropic.claude-instant-v1". This is the most accurate since it is region based. Should always be option 1
    Option2. model = "openai/gpt-4"       - model = provider/model
    Option3. model = "anthropic.claude-3" - model = model
    """
    if (
        model_with_provider in model_cost_ref
    ):  # Option 2. use model with provider, model = "openai/gpt-4"
        model = model_with_provider
    elif model in model_cost_ref:  # Option 1. use model passed, model="gpt-4"
        model = model
    elif (
        model_without_prefix in model_cost_ref
    ):  # Option 3. if user passed model="bedrock/anthropic.claude-3", use model="anthropic.claude-3"
        model = model_without_prefix

    # see this https://learn.microsoft.com/en-us/azure/ai-services/openai/concepts/models
    if call_type == "speech" or call_type == "aspeech":
        if prompt_characters is None:
            raise ValueError(
                "prompt_characters must be provided for tts calls. prompt_characters={}, model={}, custom_llm_provider={}, call_type={}".format(
                    prompt_characters,
                    model,
                    custom_llm_provider,
                    call_type,
                )
            )
        prompt_cost, completion_cost = _generic_cost_per_character(
            model=model_without_prefix,
            custom_llm_provider=custom_llm_provider,
            prompt_characters=prompt_characters,
            completion_characters=0,
            custom_prompt_cost=None,
            custom_completion_cost=0,
        )
        if prompt_cost is None or completion_cost is None:
            raise ValueError(
                "cost for tts call is None. prompt_cost={}, completion_cost={}, model={}, custom_llm_provider={}, prompt_characters={}, completion_characters={}".format(
                    prompt_cost,
                    completion_cost,
                    model_without_prefix,
                    custom_llm_provider,
                    prompt_characters,
                    completion_characters,
                )
            )
        return prompt_cost, completion_cost
    elif call_type == "arerank" or call_type == "rerank":
        return rerank_cost(
            model=model,
            custom_llm_provider=custom_llm_provider,
            billed_units=rerank_billed_units,
        )
    elif (
        call_type == "aretrieve_batch"
        or call_type == "retrieve_batch"
        or call_type == CallTypes.aretrieve_batch
        or call_type == CallTypes.retrieve_batch
    ):
        return batch_cost_calculator(
            usage=usage_block, model=model, custom_llm_provider=custom_llm_provider
        )
    elif call_type == "atranscription" or call_type == "transcription":
        return openai_cost_per_second(
            model=model,
            custom_llm_provider=custom_llm_provider,
            duration=audio_transcription_file_duration,
        )
    elif custom_llm_provider == "vertex_ai":
        cost_router = google_cost_router(
            model=model_without_prefix,
            custom_llm_provider=custom_llm_provider,
            call_type=call_type,
        )
        if cost_router == "cost_per_character":
            return google_cost_per_character(
                model=model_without_prefix,
                custom_llm_provider=custom_llm_provider,
                prompt_characters=prompt_characters,
                completion_characters=completion_characters,
                usage=usage_block,
            )
        elif cost_router == "cost_per_token":
            return google_cost_per_token(
                model=model_without_prefix,
                custom_llm_provider=custom_llm_provider,
                usage=usage_block,
            )
    elif custom_llm_provider == "anthropic":
        return anthropic_cost_per_token(model=model, usage=usage_block)
    elif custom_llm_provider == "openai":
        return openai_cost_per_token(model=model, usage=usage_block)
    elif custom_llm_provider == "databricks":
        return databricks_cost_per_token(model=model, usage=usage_block)
    elif custom_llm_provider == "fireworks_ai":
        return fireworks_ai_cost_per_token(model=model, usage=usage_block)
    elif custom_llm_provider == "azure":
        return azure_openai_cost_per_token(
            model=model, usage=usage_block, response_time_ms=response_time_ms
        )
    elif custom_llm_provider == "gemini":
        return gemini_cost_per_token(model=model, usage=usage_block)
    elif custom_llm_provider == "deepseek":
        return deepseek_cost_per_token(model=model, usage=usage_block)
    else:
        model_info = _cached_get_model_info_helper(
            model=model, custom_llm_provider=custom_llm_provider
        )

        if model_info["input_cost_per_token"] > 0:
            ## COST PER TOKEN ##
            prompt_tokens_cost_usd_dollar = (
                model_info["input_cost_per_token"] * prompt_tokens
            )
        elif (
            model_info.get("input_cost_per_second", None) is not None
            and response_time_ms is not None
        ):
            verbose_logger.debug(
                "For model=%s - input_cost_per_second: %s; response time: %s",
                model,
                model_info.get("input_cost_per_second", None),
                response_time_ms,
            )
            ## COST PER SECOND ##
            prompt_tokens_cost_usd_dollar = (
                model_info["input_cost_per_second"] * response_time_ms / 1000  # type: ignore
            )

        if model_info["output_cost_per_token"] > 0:
            completion_tokens_cost_usd_dollar = (
                model_info["output_cost_per_token"] * completion_tokens
            )
        elif (
            model_info.get("output_cost_per_second", None) is not None
            and response_time_ms is not None
        ):
            verbose_logger.debug(
                "For model=%s - output_cost_per_second: %s; response time: %s",
                model,
                model_info.get("output_cost_per_second", None),
                response_time_ms,
            )
            ## COST PER SECOND ##
            completion_tokens_cost_usd_dollar = (
                model_info["output_cost_per_second"] * response_time_ms / 1000  # type: ignore
            )

        verbose_logger.debug(
            "Returned custom cost for model=%s - prompt_tokens_cost_usd_dollar: %s, completion_tokens_cost_usd_dollar: %s",
            model,
            prompt_tokens_cost_usd_dollar,
            completion_tokens_cost_usd_dollar,
        )
        return prompt_tokens_cost_usd_dollar, completion_tokens_cost_usd_dollar


def get_replicate_completion_pricing(completion_response: dict, total_time=0.0):
    # see https://replicate.com/pricing
    # for all litellm currently supported LLMs, almost all requests go to a100_80gb
    a100_80gb_price_per_second_public = DEFAULT_REPLICATE_GPU_PRICE_PER_SECOND  # assume all calls sent to A100 80GB for now
    if total_time == 0.0:  # total time is in ms
        start_time = completion_response.get("created", time.time())
        end_time = getattr(completion_response, "ended", time.time())
        total_time = end_time - start_time

    return a100_80gb_price_per_second_public * total_time / 1000


def has_hidden_params(obj: Any) -> bool:
    return hasattr(obj, "_hidden_params")


def _get_provider_for_cost_calc(
    model: Optional[str],
    custom_llm_provider: Optional[str] = None,
) -> Optional[str]:
    if custom_llm_provider is not None:
        return custom_llm_provider
    if model is None:
        return None
    try:
        _, custom_llm_provider, _, _ = litellm.get_llm_provider(model=model)
    except Exception as e:
        verbose_logger.debug(
            f"litellm.cost_calculator.py::_get_provider_for_cost_calc() - Error inferring custom_llm_provider - {str(e)}"
        )
        return None

    return custom_llm_provider


def _select_model_name_for_cost_calc(
    model: Optional[str],
    completion_response: Optional[Any],
    base_model: Optional[str] = None,
    custom_pricing: Optional[bool] = None,
    custom_llm_provider: Optional[str] = None,
    router_model_id: Optional[str] = None,
) -> Optional[str]:
    """
    1. If custom pricing is true, return received model name
    2. If base_model is set (e.g. for azure models), return that
    3. If completion response has model set return that
    4. Check if model is passed in return that
    """

    return_model: Optional[str] = None
    region_name: Optional[str] = None
    custom_llm_provider = _get_provider_for_cost_calc(
        model=model, custom_llm_provider=custom_llm_provider
    )

    completion_response_model: Optional[str] = None
    if completion_response is not None:
        if isinstance(completion_response, BaseModel):
            completion_response_model = getattr(completion_response, "model", None)
        elif isinstance(completion_response, dict):
            completion_response_model = completion_response.get("model", None)
    hidden_params: Optional[dict] = getattr(completion_response, "_hidden_params", None)

    if custom_pricing is True:
        if router_model_id is not None and router_model_id in litellm.model_cost:
            return_model = router_model_id
        else:
            return_model = model

    if base_model is not None:
        return_model = base_model

    if completion_response_model is None and hidden_params is not None:
        if (
            hidden_params.get("model", None) is not None
            and len(hidden_params["model"]) > 0
        ):
            return_model = hidden_params.get("model", model)
    if hidden_params is not None and hidden_params.get("region_name", None) is not None:
        region_name = hidden_params.get("region_name", None)

    if return_model is None and completion_response_model is not None:
        return_model = completion_response_model

    if return_model is None and model is not None:
        return_model = model

    if (
        return_model is not None
        and custom_llm_provider is not None
        and not _model_contains_known_llm_provider(return_model)
    ):  # add provider prefix if not already present, to match model_cost
        if region_name is not None:
            return_model = f"{custom_llm_provider}/{region_name}/{return_model}"
        else:
            return_model = f"{custom_llm_provider}/{return_model}"

    return return_model


@lru_cache(maxsize=DEFAULT_MAX_LRU_CACHE_SIZE)
def _model_contains_known_llm_provider(model: str) -> bool:
    """
    Check if the model contains a known llm provider
    """
    _provider_prefix = model.split("/")[0]
    return _provider_prefix in LlmProvidersSet


def _get_usage_object(
    completion_response: Any,
) -> Optional[Usage]:
    usage_obj = cast(
        Union[Usage, ResponseAPIUsage, dict, BaseModel],
        (
            completion_response.get("usage")
            if isinstance(completion_response, dict)
            else getattr(completion_response, "get", lambda x: None)("usage")
        ),
    )

    if usage_obj is None:
        return None
    if isinstance(usage_obj, Usage):
        return usage_obj
    elif (
        usage_obj is not None
        and (isinstance(usage_obj, dict) or isinstance(usage_obj, ResponseAPIUsage))
        and ResponseAPILoggingUtils._is_response_api_usage(usage_obj)
    ):
        return ResponseAPILoggingUtils._transform_response_api_usage_to_chat_usage(
            usage_obj
        )
    elif isinstance(usage_obj, dict):
        return Usage(**usage_obj)
    elif isinstance(usage_obj, BaseModel):
        return Usage(**usage_obj.model_dump())
    else:
        verbose_logger.debug(
            f"Unknown usage object type: {type(usage_obj)}, usage_obj: {usage_obj}"
        )
        return None


def _is_known_usage_objects(usage_obj):
    """Returns True if the usage obj is a known Usage type"""
    return isinstance(usage_obj, litellm.Usage) or isinstance(
        usage_obj, ResponseAPIUsage
    )


def _infer_call_type(
    call_type: Optional[CallTypesLiteral], completion_response: Any
) -> Optional[CallTypesLiteral]:
    if call_type is not None:
        return call_type

    if completion_response is None:
        return None

    if isinstance(completion_response, ModelResponse):
        return "completion"
    elif isinstance(completion_response, EmbeddingResponse):
        return "embedding"
    elif isinstance(completion_response, TranscriptionResponse):
        return "transcription"
    elif isinstance(completion_response, HttpxBinaryResponseContent):
        return "speech"
    elif isinstance(completion_response, RerankResponse):
        return "rerank"
    elif isinstance(completion_response, ImageResponse):
        return "image_generation"
    elif isinstance(completion_response, TextCompletionResponse):
        return "text_completion"

    return call_type


def completion_cost(  # noqa: PLR0915
    completion_response=None,
    model: Optional[str] = None,
    prompt="",
    messages: List = [],
    completion="",
    total_time: Optional[float] = 0.0,  # used for replicate, sagemaker
    call_type: Optional[CallTypesLiteral] = None,
    ### REGION ###
    custom_llm_provider=None,
    region_name=None,  # used for bedrock pricing
    ### IMAGE GEN ###
    size: Optional[str] = None,
    quality: Optional[str] = None,
    n: Optional[int] = None,  # number of images
    ### CUSTOM PRICING ###
    custom_cost_per_token: Optional[CostPerToken] = None,
    custom_cost_per_second: Optional[float] = None,
    optional_params: Optional[dict] = None,
    custom_pricing: Optional[bool] = None,
    base_model: Optional[str] = None,
    standard_built_in_tools_params: Optional[StandardBuiltInToolsParams] = None,
    litellm_model_name: Optional[str] = None,
    router_model_id: Optional[str] = None,
) -> float:
    """
    Calculate the cost of a given completion call fot GPT-3.5-turbo, llama2, any litellm supported llm.

    Parameters:
        completion_response (litellm.ModelResponses): [Required] The response received from a LiteLLM completion request.

        [OPTIONAL PARAMS]
        model (str): Optional. The name of the language model used in the completion calls
        prompt (str): Optional. The input prompt passed to the llm
        completion (str): Optional. The output completion text from the llm
        total_time (float, int): Optional. (Only used for Replicate LLMs) The total time used for the request in seconds
        custom_cost_per_token: Optional[CostPerToken]: the cost per input + output token for the llm api call.
        custom_cost_per_second: Optional[float]: the cost per second for the llm api call.

    Returns:
        float: The cost in USD dollars for the completion based on the provided parameters.

    Exceptions:
        Raises exception if model not in the litellm model cost map. Register model, via custom pricing or PR - https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json


    Note:
        - If completion_response is provided, the function extracts token information and the model name from it.
        - If completion_response is not provided, the function calculates token counts based on the model and input text.
        - The cost is calculated based on the model, prompt tokens, and completion tokens.
        - For certain models containing "togethercomputer" in the name, prices are based on the model size.
        - For un-mapped Replicate models, the cost is calculated based on the total time used for the request.
    """
    try:
        call_type = _infer_call_type(call_type, completion_response) or "completion"

        if (
            (call_type == "aimage_generation" or call_type == "image_generation")
            and model is not None
            and isinstance(model, str)
            and len(model) == 0
            and custom_llm_provider == "azure"
        ):
            model = "dall-e-2"  # for dall-e-2, azure expects an empty model name
        # Handle Inputs to completion_cost
        prompt_tokens = 0
        prompt_characters: Optional[int] = None
        completion_tokens = 0
        completion_characters: Optional[int] = None
        cache_creation_input_tokens: Optional[int] = None
        cache_read_input_tokens: Optional[int] = None
        audio_transcription_file_duration: float = 0.0
        cost_per_token_usage_object: Optional[Usage] = _get_usage_object(
            completion_response=completion_response
        )
        rerank_billed_units: Optional[RerankBilledUnits] = None
        selected_model = _select_model_name_for_cost_calc(
            model=model,
            completion_response=completion_response,
            custom_llm_provider=custom_llm_provider,
            custom_pricing=custom_pricing,
            base_model=base_model,
            router_model_id=router_model_id,
        )

        potential_model_names = [selected_model]
        if model is not None:
            potential_model_names.append(model)
        for idx, model in enumerate(potential_model_names):
            try:
                verbose_logger.info(
                    f"selected model name for cost calculation: {model}"
                )

                if completion_response is not None and (
                    isinstance(completion_response, BaseModel)
                    or isinstance(completion_response, dict)
                ):  # tts returns a custom class
                    if isinstance(completion_response, dict):
                        usage_obj: Optional[
                            Union[dict, Usage]
                        ] = completion_response.get("usage", {})
                    else:
                        usage_obj = getattr(completion_response, "usage", {})
                    if isinstance(usage_obj, BaseModel) and not _is_known_usage_objects(
                        usage_obj=usage_obj
                    ):
                        setattr(
                            completion_response,
                            "usage",
                            litellm.Usage(**usage_obj.model_dump()),
                        )
                    if usage_obj is None:
                        _usage = {}
                    elif isinstance(usage_obj, BaseModel):
                        _usage = usage_obj.model_dump()
                    else:
                        _usage = usage_obj

                    if ResponseAPILoggingUtils._is_response_api_usage(_usage):
                        _usage = ResponseAPILoggingUtils._transform_response_api_usage_to_chat_usage(
                            _usage
                        ).model_dump()

                    # get input/output tokens from completion_response
                    prompt_tokens = _usage.get("prompt_tokens", 0)
                    completion_tokens = _usage.get("completion_tokens", 0)
                    cache_creation_input_tokens = _usage.get(
                        "cache_creation_input_tokens", 0
                    )
                    cache_read_input_tokens = _usage.get("cache_read_input_tokens", 0)
                    if (
                        "prompt_tokens_details" in _usage
                        and _usage["prompt_tokens_details"] != {}
                        and _usage["prompt_tokens_details"]
                    ):
                        prompt_tokens_details = _usage.get("prompt_tokens_details", {})
                        cache_read_input_tokens = prompt_tokens_details.get(
                            "cached_tokens", 0
                        )

                    total_time = getattr(completion_response, "_response_ms", 0)

                    hidden_params = getattr(completion_response, "_hidden_params", None)
                    if hidden_params is not None:
                        custom_llm_provider = hidden_params.get(
                            "custom_llm_provider", custom_llm_provider or None
                        )
                        region_name = hidden_params.get("region_name", region_name)
                        size = hidden_params.get("optional_params", {}).get(
                            "size", "1024-x-1024"
                        )  # openai default
                        quality = hidden_params.get("optional_params", {}).get(
                            "quality", "standard"
                        )  # openai default
                        n = hidden_params.get("optional_params", {}).get(
                            "n", 1
                        )  # openai default
                else:
                    if model is None:
                        raise ValueError(
                            f"Model is None and does not exist in passed completion_response. Passed completion_response={completion_response}, model={model}"
                        )
                    if len(messages) > 0:
                        prompt_tokens = token_counter(model=model, messages=messages)
                    elif len(prompt) > 0:
                        prompt_tokens = token_counter(model=model, text=prompt)
                    completion_tokens = token_counter(model=model, text=completion)

                if model is None:
                    raise ValueError(
                        f"Model is None and does not exist in passed completion_response. Passed completion_response={completion_response}, model={model}"
                    )
                if custom_llm_provider is None:
                    try:
                        model, custom_llm_provider, _, _ = litellm.get_llm_provider(
                            model=model
                        )  # strip the llm provider from the model name -> for image gen cost calculation
                    except Exception as e:
                        verbose_logger.debug(
                            "litellm.cost_calculator.py::completion_cost() - Error inferring custom_llm_provider - {}".format(
                                str(e)
                            )
                        )
                if (
                    call_type == CallTypes.image_generation.value
                    or call_type == CallTypes.aimage_generation.value
                    or call_type
                    == PassthroughCallTypes.passthrough_image_generation.value
                ):
                    ### IMAGE GENERATION COST CALCULATION ###
                    if custom_llm_provider == "vertex_ai":
                        if isinstance(completion_response, ImageResponse):
                            return vertex_ai_image_cost_calculator(
                                model=model,
                                image_response=completion_response,
                            )
                    elif custom_llm_provider == "bedrock":
                        if isinstance(completion_response, ImageResponse):
                            return bedrock_image_cost_calculator(
                                model=model,
                                size=size,
                                image_response=completion_response,
                                optional_params=optional_params,
                            )
                        raise TypeError(
                            "completion_response must be of type ImageResponse for bedrock image cost calculation"
                        )
                    else:
                        return default_image_cost_calculator(
                            model=model,
                            quality=quality,
                            custom_llm_provider=custom_llm_provider,
                            n=n,
                            size=size,
                            optional_params=optional_params,
                        )
                elif (
                    call_type == CallTypes.speech.value
                    or call_type == CallTypes.aspeech.value
                ):
                    prompt_characters = litellm.utils._count_characters(text=prompt)
                elif (
                    call_type == CallTypes.atranscription.value
                    or call_type == CallTypes.transcription.value
                ):
                    audio_transcription_file_duration = getattr(
                        completion_response, "duration", 0.0
                    )
                elif (
                    call_type == CallTypes.rerank.value
                    or call_type == CallTypes.arerank.value
                ):
                    if completion_response is not None and isinstance(
                        completion_response, RerankResponse
                    ):
                        meta_obj = completion_response.meta
                        if meta_obj is not None:
                            billed_units = meta_obj.get("billed_units", {}) or {}
                        else:
                            billed_units = {}

                        rerank_billed_units = RerankBilledUnits(
                            search_units=billed_units.get("search_units"),
                            total_tokens=billed_units.get("total_tokens"),
                        )

                        search_units = (
                            billed_units.get("search_units") or 1
                        )  # cohere charges per request by default.
                        completion_tokens = search_units
                # Calculate cost based on prompt_tokens, completion_tokens
                if (
                    "togethercomputer" in model
                    or "together_ai" in model
                    or custom_llm_provider == "together_ai"
                ):
                    # together ai prices based on size of llm
                    # get_model_params_and_category takes a model name and returns the category of LLM size it is in model_prices_and_context_window.json

                    model = get_model_params_and_category(
                        model, call_type=CallTypes(call_type)
                    )

                # replicate llms are calculate based on time for request running
                # see https://replicate.com/pricing
                elif (
                    model in litellm.replicate_models or "replicate" in model
                ) and model not in litellm.model_cost:
                    # for unmapped replicate model, default to replicate's time tracking logic
                    return get_replicate_completion_pricing(completion_response, total_time)  # type: ignore

                if model is None:
                    raise ValueError(
                        f"Model is None and does not exist in passed completion_response. Passed completion_response={completion_response}, model={model}"
                    )

                if (
                    custom_llm_provider is not None
                    and custom_llm_provider == "vertex_ai"
                ):
                    # Calculate the prompt characters + response characters
                    if len(messages) > 0:
                        prompt_string = litellm.utils.get_formatted_prompt(
                            data={"messages": messages}, call_type="completion"
                        )

                        prompt_characters = litellm.utils._count_characters(
                            text=prompt_string
                        )
                    if completion_response is not None and isinstance(
                        completion_response, ModelResponse
                    ):
                        completion_string = litellm.utils.get_response_string(
                            response_obj=completion_response
                        )
                        completion_characters = litellm.utils._count_characters(
                            text=completion_string
                        )

                (
                    prompt_tokens_cost_usd_dollar,
                    completion_tokens_cost_usd_dollar,
                ) = cost_per_token(
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    custom_llm_provider=custom_llm_provider,
                    response_time_ms=total_time,
                    region_name=region_name,
                    custom_cost_per_second=custom_cost_per_second,
                    custom_cost_per_token=custom_cost_per_token,
                    prompt_characters=prompt_characters,
                    completion_characters=completion_characters,
                    cache_creation_input_tokens=cache_creation_input_tokens,
                    cache_read_input_tokens=cache_read_input_tokens,
                    usage_object=cost_per_token_usage_object,
                    call_type=cast(CallTypesLiteral, call_type),
                    audio_transcription_file_duration=audio_transcription_file_duration,
                    rerank_billed_units=rerank_billed_units,
                )
                _final_cost = (
                    prompt_tokens_cost_usd_dollar + completion_tokens_cost_usd_dollar
                )
                _final_cost += (
                    StandardBuiltInToolCostTracking.get_cost_for_built_in_tools(
                        model=model,
                        response_object=completion_response,
                        standard_built_in_tools_params=standard_built_in_tools_params,
                        custom_llm_provider=custom_llm_provider,
                    )
                )
                return _final_cost
            except Exception as e:
                verbose_logger.debug(
                    "litellm.cost_calculator.py::completion_cost() - Error calculating cost for model={} - {}".format(
                        model, str(e)
                    )
                )
                if idx == len(potential_model_names) - 1:
                    raise e
        raise Exception(
            "Unable to calculat cost for received potential model names - {}".format(
                potential_model_names
            )
        )
    except Exception as e:
        raise e


def get_response_cost_from_hidden_params(
    hidden_params: Union[dict, BaseModel]
) -> Optional[float]:
    if isinstance(hidden_params, BaseModel):
        _hidden_params_dict = hidden_params.model_dump()
    else:
        _hidden_params_dict = hidden_params

    additional_headers = _hidden_params_dict.get("additional_headers", {})
    if (
        additional_headers
        and "llm_provider-x-litellm-response-cost" in additional_headers
    ):
        response_cost = additional_headers["llm_provider-x-litellm-response-cost"]
        if response_cost is None:
            return None
        return float(additional_headers["llm_provider-x-litellm-response-cost"])
    return None


def response_cost_calculator(
    response_object: Union[
        ModelResponse,
        EmbeddingResponse,
        ImageResponse,
        TranscriptionResponse,
        TextCompletionResponse,
        HttpxBinaryResponseContent,
        RerankResponse,
        ResponsesAPIResponse,
    ],
    model: str,
    custom_llm_provider: Optional[str],
    call_type: Literal[
        "embedding",
        "aembedding",
        "completion",
        "acompletion",
        "atext_completion",
        "text_completion",
        "image_generation",
        "aimage_generation",
        "moderation",
        "amoderation",
        "atranscription",
        "transcription",
        "aspeech",
        "speech",
        "rerank",
        "arerank",
    ],
    optional_params: dict,
    cache_hit: Optional[bool] = None,
    base_model: Optional[str] = None,
    custom_pricing: Optional[bool] = None,
    prompt: str = "",
    standard_built_in_tools_params: Optional[StandardBuiltInToolsParams] = None,
    litellm_model_name: Optional[str] = None,
    router_model_id: Optional[str] = None,
) -> float:
    """
    Returns
    - float or None: cost of response
    """
    try:
        response_cost: float = 0.0
        if cache_hit is not None and cache_hit is True:
            response_cost = 0.0
        else:
            if isinstance(response_object, BaseModel):
                response_object._hidden_params["optional_params"] = optional_params

                if hasattr(response_object, "_hidden_params"):
                    provider_response_cost = get_response_cost_from_hidden_params(
                        response_object._hidden_params
                    )
                    if provider_response_cost is not None:
                        return provider_response_cost

            response_cost = completion_cost(
                completion_response=response_object,
                model=model,
                call_type=call_type,
                custom_llm_provider=custom_llm_provider,
                optional_params=optional_params,
                custom_pricing=custom_pricing,
                base_model=base_model,
                prompt=prompt,
                standard_built_in_tools_params=standard_built_in_tools_params,
                litellm_model_name=litellm_model_name,
                router_model_id=router_model_id,
            )
        return response_cost
    except Exception as e:
        raise e


def rerank_cost(
    model: str,
    custom_llm_provider: Optional[str],
    billed_units: Optional[RerankBilledUnits] = None,
) -> Tuple[float, float]:
    """
    Returns
    - float or None: cost of response OR none if error.
    """
    _, custom_llm_provider, _, _ = litellm.get_llm_provider(
        model=model, custom_llm_provider=custom_llm_provider
    )

    try:
        config = ProviderConfigManager.get_provider_rerank_config(
            model=model,
            api_base=None,
            present_version_params=[],
            provider=LlmProviders(custom_llm_provider),
        )

        try:
            model_info: Optional[ModelInfo] = litellm.get_model_info(
                model=model, custom_llm_provider=custom_llm_provider
            )
        except Exception:
            model_info = None

        return config.calculate_rerank_cost(
            model=model,
            custom_llm_provider=custom_llm_provider,
            billed_units=billed_units,
            model_info=model_info,
        )
    except Exception as e:
        raise e


def transcription_cost(
    model: str, custom_llm_provider: Optional[str], duration: float
) -> Tuple[float, float]:
    return openai_cost_per_second(
        model=model, custom_llm_provider=custom_llm_provider, duration=duration
    )


def default_image_cost_calculator(
    model: str,
    custom_llm_provider: Optional[str] = None,
    quality: Optional[str] = None,
    n: Optional[int] = 1,  # Default to 1 image
    size: Optional[str] = "1024-x-1024",  # OpenAI default
    optional_params: Optional[dict] = None,
) -> float:
    """
    Default image cost calculator for image generation

    Args:
        model (str): Model name
        image_response (ImageResponse): Response from image generation
        quality (Optional[str]): Image quality setting
        n (Optional[int]): Number of images generated
        size (Optional[str]): Image size (e.g. "1024x1024" or "1024-x-1024")

    Returns:
        float: Cost in USD for the image generation

    Raises:
        Exception: If model pricing not found in cost map
    """
    # Standardize size format to use "-x-"
    size_str: str = size or "1024-x-1024"
    size_str = (
        size_str.replace("x", "-x-")
        if "x" in size_str and "-x-" not in size_str
        else size_str
    )

    # Parse dimensions
    height, width = map(int, size_str.split("-x-"))

    # Build model names for cost lookup
    base_model_name = f"{size_str}/{model}"
    if custom_llm_provider and model.startswith(custom_llm_provider):
        base_model_name = (
            f"{custom_llm_provider}/{size_str}/{model.replace(custom_llm_provider, '')}"
        )
    model_name_with_quality = (
        f"{quality}/{base_model_name}" if quality else base_model_name
    )

    verbose_logger.debug(
        f"Looking up cost for models: {model_name_with_quality}, {base_model_name}"
    )

    # Try model with quality first, fall back to base model name
    if model_name_with_quality in litellm.model_cost:
        cost_info = litellm.model_cost[model_name_with_quality]
    elif base_model_name in litellm.model_cost:
        cost_info = litellm.model_cost[base_model_name]
    else:
        # Try without provider prefix
        model_without_provider = f"{size_str}/{model.split('/')[-1]}"
        model_with_quality_without_provider = (
            f"{quality}/{model_without_provider}" if quality else model_without_provider
        )

        if model_with_quality_without_provider in litellm.model_cost:
            cost_info = litellm.model_cost[model_with_quality_without_provider]
        elif model_without_provider in litellm.model_cost:
            cost_info = litellm.model_cost[model_without_provider]
        else:
            raise Exception(
                f"Model not found in cost map. Tried {model_name_with_quality}, {base_model_name}, {model_with_quality_without_provider}, and {model_without_provider}"
            )

    return cost_info["input_cost_per_pixel"] * height * width * n


def batch_cost_calculator(
    usage: Usage,
    model: str,
    custom_llm_provider: Optional[str] = None,
) -> Tuple[float, float]:
    """
    Calculate the cost of a batch job
    """

    _, custom_llm_provider, _, _ = litellm.get_llm_provider(
        model=model, custom_llm_provider=custom_llm_provider
    )

    verbose_logger.info(
        "Calculating batch cost per token. model=%s, custom_llm_provider=%s",
        model,
        custom_llm_provider,
    )

    try:
        model_info: Optional[ModelInfo] = litellm.get_model_info(
            model=model, custom_llm_provider=custom_llm_provider
        )
    except Exception:
        model_info = None

    if not model_info:
        return 0.0, 0.0

    input_cost_per_token_batches = model_info.get("input_cost_per_token_batches")
    input_cost_per_token = model_info.get("input_cost_per_token")
    output_cost_per_token_batches = model_info.get("output_cost_per_token_batches")
    output_cost_per_token = model_info.get("output_cost_per_token")
    total_prompt_cost = 0.0
    total_completion_cost = 0.0
    if input_cost_per_token_batches:
        total_prompt_cost = usage.prompt_tokens * input_cost_per_token_batches
    elif input_cost_per_token:
        total_prompt_cost = (
            usage.prompt_tokens * (input_cost_per_token) / 2
        )  # batch cost is usually half of the regular token cost
    if output_cost_per_token_batches:
        total_completion_cost = usage.completion_tokens * output_cost_per_token_batches
    elif output_cost_per_token:
        total_completion_cost = (
            usage.completion_tokens * (output_cost_per_token) / 2
        )  # batch cost is usually half of the regular token cost

    return total_prompt_cost, total_completion_cost


class RealtimeAPITokenUsageProcessor:
    @staticmethod
    def collect_usage_from_realtime_stream_results(
        results: OpenAIRealtimeStreamList,
    ) -> List[Usage]:
        """
        Collect usage from realtime stream results
        """
        response_done_events: List[OpenAIRealtimeStreamResponseBaseObject] = cast(
            List[OpenAIRealtimeStreamResponseBaseObject],
            [result for result in results if result["type"] == "response.done"],
        )
        usage_objects: List[Usage] = []
        for result in response_done_events:
            usage_object = (
                ResponseAPILoggingUtils._transform_response_api_usage_to_chat_usage(
                    result["response"].get("usage", {})
                )
            )
            usage_objects.append(usage_object)
        return usage_objects

    @staticmethod
    def combine_usage_objects(usage_objects: List[Usage]) -> Usage:
        """
        Combine multiple Usage objects into a single Usage object, checking model keys for nested values.
        """
        from litellm.types.utils import (
            CompletionTokensDetails,
            PromptTokensDetailsWrapper,
            Usage,
        )

        combined = Usage()

        # Sum basic token counts
        for usage in usage_objects:
            # Handle direct attributes by checking what exists in the model
            for attr in dir(usage):
                if not attr.startswith("_") and not callable(getattr(usage, attr)):
                    current_val = getattr(combined, attr, 0)
                    new_val = getattr(usage, attr, 0)
                    if (
                        new_val is not None
                        and isinstance(new_val, (int, float))
                        and isinstance(current_val, (int, float))
                    ):
                        setattr(combined, attr, current_val + new_val)
            # Handle nested prompt_tokens_details
            if hasattr(usage, "prompt_tokens_details") and usage.prompt_tokens_details:
                if (
                    not hasattr(combined, "prompt_tokens_details")
                    or not combined.prompt_tokens_details
                ):
                    combined.prompt_tokens_details = PromptTokensDetailsWrapper()

                # Check what keys exist in the model's prompt_tokens_details
                for attr in dir(usage.prompt_tokens_details):
                    if not attr.startswith("_") and not callable(
                        getattr(usage.prompt_tokens_details, attr)
                    ):
                        current_val = getattr(combined.prompt_tokens_details, attr, 0)
                        new_val = getattr(usage.prompt_tokens_details, attr, 0)
                        if new_val is not None:
                            setattr(
                                combined.prompt_tokens_details,
                                attr,
                                current_val + new_val,
                            )

            # Handle nested completion_tokens_details
            if (
                hasattr(usage, "completion_tokens_details")
                and usage.completion_tokens_details
            ):
                if (
                    not hasattr(combined, "completion_tokens_details")
                    or not combined.completion_tokens_details
                ):
                    combined.completion_tokens_details = CompletionTokensDetails()

                # Check what keys exist in the model's completion_tokens_details
                for attr in dir(usage.completion_tokens_details):
                    if not attr.startswith("_") and not callable(
                        getattr(usage.completion_tokens_details, attr)
                    ):
                        current_val = getattr(
                            combined.completion_tokens_details, attr, 0
                        )
                        new_val = getattr(usage.completion_tokens_details, attr, 0)
                        if new_val is not None:
                            setattr(
                                combined.completion_tokens_details,
                                attr,
                                current_val + new_val,
                            )

        return combined

    @staticmethod
    def collect_and_combine_usage_from_realtime_stream_results(
        results: OpenAIRealtimeStreamList,
    ) -> Usage:
        """
        Collect and combine usage from realtime stream results
        """
        collected_usage_objects = (
            RealtimeAPITokenUsageProcessor.collect_usage_from_realtime_stream_results(
                results
            )
        )
        combined_usage_object = RealtimeAPITokenUsageProcessor.combine_usage_objects(
            collected_usage_objects
        )
        return combined_usage_object


def handle_realtime_stream_cost_calculation(
    results: OpenAIRealtimeStreamList,
    combined_usage_object: Usage,
    custom_llm_provider: str,
    litellm_model_name: str,
) -> float:
    """
    Handles the cost calculation for realtime stream responses.

    Pick the 'response.done' events. Calculate total cost across all 'response.done' events.

    Args:
        results: A list of OpenAIRealtimeStreamBaseObject objects
    """
    received_model = None
    potential_model_names = []
    for result in results:
        if result["type"] == "session.created":
            received_model = cast(OpenAIRealtimeStreamSessionEvents, result)["session"][
                "model"
            ]
            potential_model_names.append(received_model)

    potential_model_names.append(litellm_model_name)
    input_cost_per_token = 0.0
    output_cost_per_token = 0.0

    for model_name in potential_model_names:
        try:
            _input_cost_per_token, _output_cost_per_token = generic_cost_per_token(
                model=model_name,
                usage=combined_usage_object,
                custom_llm_provider=custom_llm_provider,
            )
        except Exception:
            continue
        input_cost_per_token += _input_cost_per_token
        output_cost_per_token += _output_cost_per_token
        break  # exit if we find a valid model
    total_cost = input_cost_per_token + output_cost_per_token

    return total_cost
