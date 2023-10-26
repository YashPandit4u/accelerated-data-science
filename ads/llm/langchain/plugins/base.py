#!/usr/bin/env python
# -*- coding: utf-8 -*--

# Copyright (c) 2023 Oracle and/or its affiliates.
# Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl/

from enum import Enum
from typing import Any, Dict, List, Optional

from langchain.llms.base import LLM
from pydantic import BaseModel, root_validator
from ads.common.auth import default_signer
from ads.config import COMPARTMENT_OCID


class StrEnum(str, Enum):
    """Enum with string members
    https://docs.python.org/3.11/library/enum.html#enum.StrEnum
    """

    # Pydantic uses Python's standard enum classes to define choices.
    # https://docs.pydantic.dev/latest/api/standard_library_types/#enum


class BaseLLM(LLM):
    """Base OCI LLM class. Contains common attributes."""

    max_tokens: int = 256
    """Denotes the number of tokens to predict per generation."""

    temperature: float = 0.1
    """A non-negative float that tunes the degree of randomness in generation."""

    k: int = 0
    """Number of most likely tokens to consider at each step."""

    p: int = 0.9
    """Total probability mass of tokens to consider at each step."""

    stop: Optional[List[str]] = None
    """Stop words to use when generating. Model output is cut off at the first occurrence of any of these substrings."""


class GenerativeAiClientModel(BaseModel):
    client: Any  #: :meta private:
    """OCI GenerativeAiClient."""

    compartment_id: str
    """Compartment ID of the caller."""

    @root_validator()
    def validate_environment(  # pylint: disable=no-self-argument
        cls, values: Dict
    ) -> Dict:
        """Validate that python package exists in environment."""
        try:
            # Import the GenerativeAIClient here so that there will be no error when user import ads.llm
            # and the install OCI SDK does not support generative AI service yet.
            from oci.generative_ai import GenerativeAiClient
        except ImportError as ex:
            raise ImportError(
                "Could not import GenerativeAIClient from oci. "
                "The OCI SDK installed does not support generative AI service."
            ) from ex
        # Initialize client only if user does not pass in client.
        # Users may choose to initialize the OCI client by themselves and pass it into this model.
        if not values.get("client"):
            client_kwargs = values["client_kwargs"] or {}
            values["client"] = GenerativeAiClient(**default_signer(), **client_kwargs)
        # Set default compartment ID
        if "compartment_id" not in values and COMPARTMENT_OCID:
            values["compartment_id"] = COMPARTMENT_OCID

        return values
