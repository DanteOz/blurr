# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/04_data-question-answering.ipynb (unless otherwise specified).

__all__ = ['find_answer_token_idxs', 'QuestionAnsweringPreprocessor', 'QuestionAnswerTextInput',
           'QABatchTokenizeTransform']

# Cell
import ast
from functools import reduce

from datasets import Dataset
from fastcore.all import *
from fastai.data.block import DataBlock, CategoryBlock, ColReader, ColSplitter
from fastai.imports import *
from fastai.losses import CrossEntropyLossFlat
from fastai.torch_core import *
from fastai.torch_imports import *
from transformers import AutoModelForQuestionAnswering, logging, PretrainedConfig, PreTrainedTokenizerBase, PreTrainedModel

from ..utils import BLURR
from .core import TextInput, BatchDecodeTransform, BatchTokenizeTransform, Preprocessor, first_blurr_tfm

logging.set_verbosity_error()


# Cell
def find_answer_token_idxs(start_char_idx, end_char_idx, offset_mapping, qst_mask):
    # mask the question tokens so they aren't included in the search
    masked_offset_mapping = offset_mapping.clone()
    masked_offset_mapping[qst_mask] = tensor([-100, -100])

    # based on the character start/end index, see if we can find the span of tokens in the `offset_mapping`
    starts = torch.where((masked_offset_mapping[:, 0] == start_char_idx) | (masked_offset_mapping[:, 1] == start_char_idx))[0]
    ends = torch.where((masked_offset_mapping[:, 0] <= end_char_idx) & (masked_offset_mapping[:, 1] >= end_char_idx))[0]

    if len(starts) > 0 and len(ends) > 0:
        for s in starts:
            if masked_offset_mapping[s][0] <= start_char_idx:
                start = s

        for e in ends:
            if e >= s and masked_offset_mapping[e][1] >= end_char_idx:
                end = e

        if end < len(masked_offset_mapping):
            return (start, end)

    # if neither star or end is found, or the end token is part of this chunk, consider the answer not found
    return (tensor(0), tensor(0))


# Cell
class QuestionAnsweringPreprocessor(Preprocessor):
    def __init__(
        self,
        # A Hugging Face tokenizer
        hf_tokenizer: PreTrainedTokenizerBase,
        # The number of examples to process at a time
        batch_size: int = 1000,
        # The unique identifier in the dataset. If not specified and "return_overflowing_tokens": True, an "_id" attribute
        # will be added to your dataset with its value a unique, sequential integer, assigned to each record
        id_attr: Optional[str] = None,
        # The attribute in your dataset that contains the context (where the answer is included) (default: 'context')
        ctx_attr: str = "context",
        # The attribute in your dataset that contains the question being asked (default: 'question')
        qst_attr: str = "question",
        # The attribute in your dataset that contains the actual answer (default: 'answer_text')
        ans_attr: str = "answer_text",
        # The attribute in your dataset that contains the actual answer (default: 'answer_text')
        ans_start_char_idx: str = "ans_start_char_idx",
        # The attribute in your dataset that contains the actual answer (default: 'answer_text')
        ans_end_char_idx: str = "ans_end_char_idx",
        # The attribute that should be created if your are processing individual training and validation
        # datasets into a single dataset, and will indicate to which each example is associated
        is_valid_attr: Optional[str] = "is_valid",
        # Tokenization kwargs that will be applied with calling the tokenizer (default: {"return_overflowing_tokens": True})
        tok_kwargs: dict = {"return_overflowing_tokens": True},
    ):
        # these values are mandatory
        tok_kwargs["return_offsets_mapping"] = True  # allows us to map tokens -> raw characters
        tok_kwargs["padding"] = tok_kwargs.get("padding", True)
        tok_kwargs["return_tensors"] = "pt"

        # shift the question and context appropriately based on the tokenizers padding strategy
        if hf_tokenizer.padding_side == "right":
            tok_kwargs["truncation"] = "only_second"
            text_attrs = [qst_attr, ctx_attr]
        else:
            tok_kwargs["truncation"] = "only_first"
            text_attrs = [ctx_attr, qst_attr]

        super().__init__(hf_tokenizer, batch_size, text_attrs=text_attrs, tok_kwargs=tok_kwargs)

        self.id_attr = id_attr
        self.qst_attr, self.ctx_attr = qst_attr, ctx_attr
        self.ans_attr, self.ans_start_char_idx, self.ans_end_char_idx = ans_attr, ans_start_char_idx, ans_end_char_idx
        self.is_valid_attr = is_valid_attr

    def process_df(self, training_df: pd.DataFrame, validation_df: Optional[pd.DataFrame] = None):
        df = super().process_df(training_df, validation_df)

        # a unique Id for each example is required to properly score question answering results when chunking long
        # documents (e.g., return_overflowing_tokens=True)
        if self.id_attr is None and self.tok_kwargs.get("return_overflowing_tokens", False):
            df.insert(0, "_id", range(len(df)))

        proc_data = []
        for row_idx, row in df.iterrows():
            # fetch data elements required to build a modelable dataset
            inputs = self._tokenize_function(row)
            ans_text, start_char_idx, end_char_idx = row[self.ans_attr], row[self.ans_start_char_idx], row[self.ans_end_char_idx] + 1

            # if "return_overflowing_tokens = True", our BatchEncoding will include an "overflow_to_sample_mapping" list
            overflow_mapping = inputs["overflow_to_sample_mapping"] if ("overflow_to_sample_mapping" in inputs) else [0]

            for idx in range(len(overflow_mapping)):
                # update the targets: is_found (s[1]), answer start token index (s[2]), and answer end token index (s[3])
                qst_mask = [i != 1 if self.hf_tokenizer.padding_side == "right" else i != 0 for i in inputs.sequence_ids(idx)]
                start, end  = find_answer_token_idxs(start_char_idx, end_char_idx, inputs["offset_mapping"][idx], qst_mask)

                overflow_row = row.copy()
                overflow_row[self.ans_end_char_idx] = end_char_idx
                overflow_row["ans_start_token_idx"] = start.item()
                overflow_row["ans_end_token_idx"] = end.item()

                for k in inputs.keys():
                    overflow_row[k] = inputs[k][idx].numpy()

                proc_data.append(overflow_row)

        return pd.DataFrame(proc_data)

    def process_hf_dataset(self, training_ds: Dataset, validation_ds: Optional[Dataset] = None):
        ds = super().process_hf_dataset(training_ds, validation_ds)

        # return the pre-processed DataFrame
        return ds


# Cell
class QuestionAnswerTextInput(TextInput):
    pass


# Cell
class QABatchTokenizeTransform(BatchTokenizeTransform):
    def __init__(
        self,
        # The abbreviation/name of your Hugging Face transformer architecture (e.b., bert, bart, etc..)
        hf_arch: str,
        # A specific configuration instance you want to use
        hf_config: PretrainedConfig,
        # A Hugging Face tokenizer
        hf_tokenizer: PreTrainedTokenizerBase,
        # A Hugging Face model
        hf_model: PreTrainedModel,
        # Contray to other NLP tasks where batch-time tokenization (`is_pretokenized` = False) is the default, with
        # extractive question answering pre-processing is required, and as such we set it to True here
        is_pretokenized: bool = True,
        # The token ID that should be ignored when calculating the loss
        ignore_token_id=CrossEntropyLossFlat().ignore_index,
        # To control the length of the padding/truncation. It can be an integer or None,
        # in which case it will default to the maximum length the model can accept. If the model has no
        # specific maximum input length, truncation/padding to max_length is deactivated.
        # See [Everything you always wanted to know about padding and truncation](https://huggingface.co/transformers/preprocessing.html#everything-you-always-wanted-to-know-about-padding-and-truncation)
        max_length: int = None,
        # To control the `padding` applied to your `hf_tokenizer` during tokenization. If None, will default to
        # `False` or `'do_not_pad'.
        # See [Everything you always wanted to know about padding and truncation](https://huggingface.co/transformers/preprocessing.html#everything-you-always-wanted-to-know-about-padding-and-truncation)
        padding: Union[bool, str] = True,
        # To control `truncation` applied to your `hf_tokenizer` during tokenization. If None, will default to
        # `False` or `do_not_truncate`.
        # See [Everything you always wanted to know about padding and truncation](https://huggingface.co/transformers/preprocessing.html#everything-you-always-wanted-to-know-about-padding-and-truncation)
        truncation: Union[bool, str] = "only_second",
        # The `is_split_into_words` argument applied to your `hf_tokenizer` during tokenization. Set this to `True`
        # if your inputs are pre-tokenized (not numericalized)
        is_split_into_words: bool = False,
        # Any other keyword arguments you want included when using your `hf_tokenizer` to tokenize your inputs.
        # Since extractive requires pre-tokenized input_ids, we default this to not include any "special" tokens as they are already
        # included in the pre-processed input_ids
        tok_kwargs: dict = {"add_special_tokens": False},
        # Keyword arguments to apply to `BatchTokenizeTransform`
        **kwargs
    ):

        # "return_special_tokens_mask" and "return_offsets_mapping" are mandatory for extractive QA in blurr
        tok_kwargs = { **tok_kwargs, **{"return_special_tokens_mask": True, "return_offsets_mapping": True}}

        super().__init__(
            hf_arch,
            hf_config,
            hf_tokenizer,
            hf_model,
            is_pretokenized=is_pretokenized,
            ignore_token_id=ignore_token_id,
            max_length=max_length,
            padding=padding,
            truncation=truncation,
            is_split_into_words=is_split_into_words,
            tok_kwargs=tok_kwargs,
            **kwargs
        )

    def encodes(self, samples):
        samples, batch_encoding = super().encodes(samples, return_batch_encoding=True)

        if self.is_pretokenized:
            return samples

        for idx, s in enumerate(samples):
            # cls_index: location of CLS token (used by xlnet and xlm); is a list.index(value) for pytorch tensor's
            s[0]["cls_index"] = (s[0]["input_ids"] == self.hf_tokenizer.cls_token_id).nonzero()[0]
            # p_mask: mask with 1 for token than cannot be in the answer, else 0 (used by xlnet and xlm)
            s[0]["p_mask"] = s[0]["special_tokens_mask"]

        return samples


# Cell
@typedispatch
def show_batch(
    # This typedispatched `show_batch` will be called for `QuestionAnswerTextInput` typed inputs
    x: QuestionAnswerTextInput,
    # Your targets
    y,
    # Your raw inputs/targets
    samples,
    # Your `DataLoaders`. This is required so as to get at the Hugging Face objects for
    # decoding them into something understandable
    dataloaders,
    # Your `show_batch` context
    ctxs=None,
    # The maximum number of items to show
    max_n=6,
    # Any truncation your want applied to your decoded inputs
    trunc_at=None,
    # Any other keyword arguments you want applied to `show_batch`
    **kwargs
):
    # grab our tokenizer
    tfm = first_blurr_tfm(dataloaders, tfms=[QABatchTokenizeTransform])
    hf_tokenizer = tfm.hf_tokenizer

    res = L()
    for sample, input_ids, start, end in zip(samples, x, *y):
        txt = hf_tokenizer.decode(sample[0], skip_special_tokens=True)[:trunc_at]
        found = (start.item() != 0 and end.item() != 0)
        ans_text = hf_tokenizer.decode(input_ids[start:end], skip_special_tokens=False)
        res.append((txt, found, (start.item(), end.item()), ans_text))

    display_df(pd.DataFrame(res, columns=["text", "found", "start/end", "answer"])[:max_n])
    return ctxs
