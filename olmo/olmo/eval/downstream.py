import abc
import logging
import re
from typing import Any, ClassVar, Dict, List, Optional, Sequence, Union

import datasets
import torch
import torch.nn.functional as F
from sklearn.metrics import f1_score
from torchmetrics import Metric

from ..tokenizer import Tokenizer

from tqdm.auto import tqdm

log = logging.getLogger(__name__)


class ICLMetric(Metric):
    # update method does not require access to global metric state
    full_state_update: bool = False

    def __init__(self, metric_type="acc") -> None:
        """metric_type: f1, acc, len_norm, pmi_dc, ce_loss, full_ce_loss"""
        super().__init__(sync_on_compute=True)

        self.metric_type = metric_type

        self.add_state("loglikelihoods", default=[], dist_reduce_fx=None)
        self.add_state("labels", default=[], dist_reduce_fx=None)

    def reset(
        self,
    ):
        self.loglikelihoods = []
        self.labels = []

    def update(self, batch: Dict[str, Any], lm_logits: torch.Tensor, dc_lm_logits=None):
        lm_logits = F.log_softmax(lm_logits, dim=-1)

        if self.metric_type == "pmi_dc":
            assert dc_lm_logits is not None, "PMI_DC acc type selected but no domain conditional logits provided"

        for idx, (doc_id, cont_id) in enumerate(zip(batch["doc_id"], batch["cont_id"])):
            if batch["ctx_len"][idx] < 0:
                print(f"Bad batch ctx_len {batch['ctx_len'][idx]}")
                continue
            if batch["cont_len"][idx] > lm_logits.shape[1]:
                print(f"Bad batch cont_len {batch['cont_len'][idx]}")
                continue
            # [cont_len]: continuation is padded for batching
            cont_tokens = batch["continuation"][idx][: batch["cont_len"][idx]]
            # get logits from LM for the continuation: [cont_len, vocab]
            # batch['input_ids'][idx] -> ctx + cont + padding
            # -1 in both indices: lm_logits will be left shited 1 pos as 0th pos in input generates next token in the 0th pos of lm_logits
            lm_cont_logits = lm_logits[idx][
                batch["ctx_len"][idx] - 1 : batch["ctx_len"][idx] + batch["cont_len"][idx] - 1
            ]

            log_likelihood: torch.Tensor
            if self.metric_type == "pmi_dc":
                assert dc_lm_logits is not None
                # get domain conditional continuation logits: [cont_len, vocab]
                dc_lm_cont_logits = dc_lm_logits[idx][
                    batch["dc_len"][idx] - 1 : batch["dc_len"][idx] + batch["cont_len"][idx] - 1
                ]

                # gather log-probs at continuation token indices but divide by domain conditional prob
                log_likelihood = (
                    torch.gather(lm_cont_logits, 1, cont_tokens.unsqueeze(-1)).sum()
                    / torch.gather(dc_lm_cont_logits, 1, cont_tokens.unsqueeze(-1)).sum()
                )
            elif self.metric_type == "acc" or self.metric_type == "f1":
                # gather log-probs at continuation token indices
                log_likelihood = torch.gather(lm_cont_logits, 1, cont_tokens.unsqueeze(-1)).sum()
            elif self.metric_type == "len_norm":
                log_likelihood = (
                    torch.gather(lm_cont_logits, 1, cont_tokens.unsqueeze(-1)).sum() / batch["cont_str_len"][idx]
                )
            elif self.metric_type == "ce_loss":
                log_likelihood = -torch.gather(lm_cont_logits, 1, cont_tokens.unsqueeze(-1)).mean()
            elif self.metric_type == "ctx_ce_loss":
                lm_ctx_logits = lm_logits[idx][: batch["ctx_len"][idx] - 1]
                ctx_tokens = batch["input_ids"][idx][1 : batch["ctx_len"][idx]]
                log_likelihood = -torch.gather(lm_ctx_logits, 1, ctx_tokens.unsqueeze(-1)).mean()
            else:
                raise ValueError(self.metric_type)

            # because metric states cannot be dict/list of tuples, store this tuple as tensor: (doc_id, cont_id, metric_state)
            self.loglikelihoods.append(
                torch.Tensor((doc_id, cont_id, log_likelihood)).to(batch["continuation"][idx].device)
            )
            self.labels.append(
                torch.LongTensor((doc_id, cont_id, batch["label_id"][idx])).to(batch["label_id"][idx].device)
            )

    def compute(self) -> torch.Tensor:
        # states should have been synced from all accelerators at this point
        # account for duplicates here because of DistributedSampler compensating for drop_last=False
        loglikelihood_dict: Dict[int, Dict[int, float]] = {}
        label_dict = {}

        # collect labels
        for doc_id, cont_id, label_id in self.labels:
            if doc_id.item() not in label_dict:
                label_dict[doc_id.item()] = label_id.item()

        # collect loglikelihoods
        for doc_id, cont_id, loglikelihood in self.loglikelihoods:
            if int(doc_id.item()) not in loglikelihood_dict:
                loglikelihood_dict[int(doc_id.item())] = {}

            if int(cont_id.item()) not in loglikelihood_dict[int(doc_id.item())]:
                loglikelihood_dict[int(doc_id.item())][int(cont_id.item())] = loglikelihood

        # compute acc
        correct = []
        preds: Optional[List[float]] = None
        labels: Optional[List[int]] = None
        if self.metric_type == "f1":
            preds = []
            labels = []

        for doc_id in loglikelihood_dict:
            # each doc_id might have a different number of continuation
            num_continuations = len(loglikelihood_dict[doc_id].keys())
            loglikelihoods = torch.tensor([-float("inf")] * num_continuations)

            skip_document = False
            for cont_id in loglikelihood_dict[doc_id]:
                try:
                    loglikelihoods[cont_id] = loglikelihood_dict[doc_id][cont_id]
                except IndexError:
                    # We didn't process all of the continuations, so skip this document.
                    skip_document = True
                    break

            if skip_document:
                continue
            if self.metric_type == "ce_loss" or self.metric_type == "ctx_ce_loss":
                correct.append(loglikelihoods[0])  # Only one answer is scored
            else:
                correct.append(1.0 if torch.argmax(loglikelihoods).item() == label_dict[doc_id] else 0.0)

            if self.metric_type == "f1":
                assert preds is not None
                assert labels is not None
                preds.append(torch.argmax(loglikelihoods).item())
                labels.append(label_dict[doc_id])

        if self.metric_type == "f1":
            assert preds is not None
            assert labels is not None
            # for NLI tasks, continuations are yes, no, neither, so idx=0 assigned to pos label
            score = f1_score(labels, preds, pos_label=0)
        else:
            score = sum(correct) / len(correct)

        return torch.tensor(score)


class OlmoDataset(metaclass=abc.ABCMeta):
    """Only supports zero-shot for now."""

    metric_type: ClassVar[str]

    def __init__(
        self,
        tokenizer: Tokenizer,
        dataset_path: str,
        dataset_name: Union[str, Sequence[str], None] = None,
        model_ctx_len: int = 2048,
        split="validation",
        prompts=[None],  # List of prompt variants to use
        sft: bool = False,
        sft_use_label: bool = True,
    ):
        super().__init__()

        self.tokenizer = tokenizer
        self.dataset_path = dataset_path
        self.dataset_name = dataset_name
        self.model_ctx_len = model_ctx_len
        self.prompts = prompts
        self.current_prompt = None
        self.log_instances = 0  # Set to > 0 to log the first few instances as a sanity check
        self.sft = sft
        self.sft_use_label = sft_use_label

        self.samples: List[Dict[str, Any]] = []
        dataset_names: Sequence[Optional[str]]
        if isinstance(dataset_name, str) or dataset_name is None:
            dataset_names = [dataset_name]
        else:
            dataset_names = dataset_name

        dataset_list = []
        for ds_name in dataset_names:
            dataset = datasets.load_dataset(
                path=self.dataset_path,
                name=ds_name,
                split=split,
                trust_remote_code=True,
            )
            if "cais" in self.dataset_path:
                # for i, data in enumerate(dataset):
                dataset = dataset["train"]
                dataset = datasets.Dataset.from_list(dataset)
            dataset_list.append(dataset)
        self.dataset = datasets.concatenate_datasets(dataset_list)

    def __getitem__(self, index):
        raise NotImplementedError

    def __len__(self):
        raise NotImplementedError

    def pad_tokens_until_max(self, tokens, max_len=2048):
        """truncate from left if len(tokens) > model_ctx_len, max_len is not considered then
        queries are already truncated at max length of model_ctx_len
        this acts as additional check for all types of sequences in the batch
        """
        if len(tokens) > self.model_ctx_len:
            return tokens[-self.model_ctx_len :]
        else:
            # pad to max_len, but check again if this padding exceeded self.model_ctx_len
            # this time truncate from right side of the sequence because additional padding caused len(tokens) > self.model_ctx_len
            tokens = tokens + [self.tokenizer.pad_token_id] * (max_len - len(tokens))

            if len(tokens) > self.model_ctx_len:
                tokens = tokens[: self.model_ctx_len]

            return tokens

    def token_encode(self, string: str) -> List[int]:
        return self.tokenizer.base_tokenizer.encode(string, add_special_tokens=False).ids
        # return self.tokenizer._tokenizer.encode(string, add_special_tokens=False).ids
        

    def token_decode(self, tokens: List[int]) -> str:
        return self.tokenizer.decode(tokens)

    def prep_examples(self):
        """Append doc_ids to each example so that they are processed together in the metric"""
        raise NotImplementedError

    def collate_fn(self, data):
        raise NotImplementedError

    @abc.abstractmethod
    def doc_to_text(self, doc) -> str:
        """Match EAI eval harness
        returns a single context string
        """
        raise NotImplementedError

    # @abc.abstractmethod
    def doc_to_continuations(self, doc) -> List[str]:
        """Match EAI eval harness
        returns a list of continuations
        """
        raise NotImplementedError

    def doc_to_solution(self, doc) -> List[str]:
        """For code evaluation, returns a list of solutions
        returns a list of solutions
        """
        raise NotImplementedError

    # @abc.abstractmethod
    def doc_to_label(self, doc) -> int:
        """Match EAI eval harness
        returns continuation id which corresponds to true label
        """
        raise NotImplementedError

    def doc_to_domain_conditional(self, doc) -> str:
        """Provide string for domain conditional normalization
        by default its blank string, continuation normalized by prob conditioned on a blank
        """
        del doc
        return " "

    def doc_to_task_id(self, doc) -> str:
        """Provide task id for the document"""
        return doc["task_id"]

    def doc_to_sft(self, doc):
        sft_example = self.doc_to_text(doc) + self.doc_to_continuations(doc)[self.doc_to_label(doc)]
        return sft_example

    def doc_to_sft_without_answer(self, doc):
        return self.doc_to_text(doc)


class ICLMultiChoiceTaskDataset(OlmoDataset):
    def __init__(
        self,
        tokenizer: Tokenizer,
        dataset_path: str,
        dataset_name: Union[str, Sequence[str], None] = None,
        model_ctx_len: int = 2048,
        split="validation",
        prompts=[None],
        sft: bool = False,
        sft_use_label: bool = True,
        filter_fn=None,
    ):
        super().__init__(tokenizer, dataset_path, dataset_name, model_ctx_len, split, prompts, sft, sft_use_label)
        if filter_fn is not None:
            self.dataset = self.dataset.filter(filter_fn)

        if len(tokenizer.encode(" yes")) == len(tokenizer.encode("yes")):
            self.add_prefix_space = True  # tokenizer needs prefix space for labels
        else:
            self.add_prefix_space = False

        self.prep_examples()

    def prep_examples(self):
        token_count = 0
        for doc_id, doc in tqdm(enumerate(self.dataset)):
            for prompt in self.prompts:
                self.current_prompt = prompt
                # from EAI harness
                # how this all works:
                #          CTX      CONT
                # inp    0 1 2 3|4 5 6 7 8 9   <- last token is deleted by inp[:, :-1]
                # gpt2    \               \
                # logits   1 2 3|4 5 6 7 8 9   <- the ctx half gets tossed out by the
                # cont_toks      4 5 6 7 8 9      [:, -len(continuation_enc):, :self.vocab_size] slice

                continuations = self.doc_to_continuations(doc)
                label_id = self.doc_to_label(doc)
                doc_text = self.doc_to_text(doc)
                ctx = self.token_encode(doc_text)

                if self.sft:
                    if self.sft_use_label:
                        sft_text = self.doc_to_sft(doc)
                    else:
                        sft_text = self.doc_to_sft_without_answer(doc)
                    sft = self.token_encode(sft_text)
                    token_count += len(sft)
                    # form a sample (Hack-y)
                    # TODO: add mask to only supervise the continuation?
                    self.samples.append({"doc_id": doc_id, "label_id": label_id, "sft": sft})

                else:
                    dc = self.token_encode(self.doc_to_domain_conditional(doc))
                    if self.log_instances > 0:
                        self.log_instances -= 1
                        ds_name = self.dataset_name
                        if isinstance(ds_name, list):
                            ds_name = ds_name[0]
                        log.info(
                            f"Sample doc from ({self.dataset_path}, {ds_name}, {self.current_prompt}):"
                            + f"\ndoc_text: {doc_text}\ncontinuations: {continuations}"
                        )

                    for cont_id, continuation_str in enumerate(continuations):
                        cont_str_len = len(continuation_str)  # - 1  # continuation contain leading blank
                        continuation = self.token_encode(continuation_str)

                        # query, remove last token from continuation, truncate from left is longer than model ctx length
                        query = ctx + continuation[:-1]
                        query = query[-self.model_ctx_len :]
                        # this will be different from len(ctx) when truncated by model_ctx_len
                        actual_ctx_len = len(query) - len(continuation) + 1

                        # get domain conditional query
                        # we don't expect this to be longer than self.model_ctx_len and it won't make sense to truncate from left
                        dc_query = dc + continuation[:-1]

                        # form a sample
                        self.samples.append(
                            {
                                "doc_id": doc_id,
                                "cont_id": cont_id,
                                "ctx": ctx,
                                "continuation": continuation,
                                "ctx_len": actual_ctx_len,
                                "dc_len": len(dc),
                                "cont_len": len(
                                    continuation
                                ),  # even if query has last token removed, LM will output same cont len
                                "cont_str_len": cont_str_len,
                                "query": query,  # remove last token from continuation
                                "dc_query": dc_query,
                                "label_id": label_id,
                            }
                        )
        print(f"Total tokens for SFT: {token_count}")

    def sft_collate_fn(self, data):
        # pad to max length
        # 'ctx', 'continuation', 'query' can all have variable length
        max_sft_len = self.model_ctx_len

        for sample in data:
            if len(sample["sft"]) > max_sft_len:
                max_sft_len = len(sample["sft"])

        doc_ids = []
        label_ids = []
        sfts = []

        # pad according to max_lengths
        for sample in data:
            doc_ids.append(sample["doc_id"])
            label_ids.append(sample["label_id"])
            sfts.append(torch.LongTensor(self.pad_tokens_until_max(sample["sft"], max_len=max_sft_len)))

        batch = {
            "doc_id": torch.LongTensor(doc_ids),
            "input_ids": torch.stack(sfts),
            "label_id": torch.LongTensor(label_ids),
        }
        return batch

    def collate_fn(self, data):
        if self.sft:
            return self.sft_collate_fn(data)

        # pad to max length
        # 'ctx', 'continuation', 'query' can all have variable length
        max_ctx_len = 0
        max_cont_len = 0
        max_query_len = 0
        max_dc_query_len = 0

        for sample in data:
            if len(sample["ctx"]) > max_ctx_len:
                max_ctx_len = len(sample["ctx"])

            if len(sample["continuation"]) > max_cont_len:
                max_cont_len = len(sample["continuation"])

            if len(sample["query"]) > max_query_len:
                max_query_len = len(sample["query"])

            if len(sample["dc_query"]) > max_dc_query_len:
                max_dc_query_len = len(sample["dc_query"])

        doc_ids = []
        cont_ids = []
        ctxs = []
        continuations = []
        ctx_lens = []
        dc_lens = []
        cont_lens = []
        cont_str_lens = []
        queries = []
        dc_queries = []
        label_ids = []

        # pad according to max_lengths
        for sample in data:
            doc_ids.append(sample["doc_id"])
            cont_ids.append(sample["cont_id"])

            ctxs.append(torch.LongTensor(self.pad_tokens_until_max(sample["ctx"], max_len=max_ctx_len)))
            continuations.append(
                torch.LongTensor(self.pad_tokens_until_max(sample["continuation"], max_len=max_cont_len))
            )

            ctx_lens.append(sample["ctx_len"])
            dc_lens.append(sample["dc_len"])
            cont_lens.append(sample["cont_len"])
            cont_str_lens.append(sample["cont_str_len"])

            queries.append(torch.LongTensor(self.pad_tokens_until_max(sample["query"], max_len=max_query_len)))
            dc_queries.append(
                torch.LongTensor(self.pad_tokens_until_max(sample["dc_query"], max_len=max_dc_query_len))
            )

            label_ids.append(sample["label_id"])

        batch = {
            "doc_id": torch.LongTensor(doc_ids),
            "cont_id": torch.LongTensor(cont_ids),
            "ctx": torch.stack(ctxs),
            "continuation": torch.stack(continuations),
            "ctx_len": torch.LongTensor(ctx_lens),
            "dc_len": torch.LongTensor(dc_lens),
            "cont_len": torch.LongTensor(cont_lens),  # since query has last token removed from continuation
            "cont_str_len": torch.LongTensor(cont_str_lens),
            "input_ids": torch.stack(queries),
            "dc_input_ids": torch.stack(dc_queries),
            "label_id": torch.LongTensor(label_ids),
        }

        return batch

    def __getitem__(self, index):
        return self.samples[index]

    def __len__(self):
        return len(self.samples)


class PIQA(ICLMultiChoiceTaskDataset):
    """PIQA sends context in the following fashion: "Question: GOAL\nAnswer:"
    space added as prefix to each continuation

    implement PMI_DC

    {
        'goal': "How do I ready a guinea pig cage for it's new occupants?",
        'sol1': 'Provide the guinea pig with a cage full of a few inches of bedding made of ripped paper strips, you will also need to supply it with a water bottle and a food dish.',
        'sol2': 'Provide the guinea pig with a cage full of a few inches of bedding made of ripped jeans material, you will also need to supply it with a water bottle and a food dish.',
        'label': 0
    }
    """

    metric_type = "len_norm"

    def __init__(
        self,
        tokenizer,
        dataset_path="piqa",
        dataset_name=None,
        sft: bool = False,
        sft_use_label: bool = True,
        model_ctx_len: int = 2048,
        split: str = "validation",
    ):
        super().__init__(
            tokenizer=tokenizer,
            dataset_path=dataset_path,
            dataset_name=dataset_name,
            sft=sft,
            sft_use_label=sft_use_label,
            model_ctx_len=model_ctx_len,
            split=split,
        )

    def doc_to_text(self, doc):
        return "Question: " + doc["goal"] + "\nAnswer:"

    def doc_to_continuations(self, doc):
        if self.add_prefix_space:
            return [" " + doc["sol1"], " " + doc["sol2"]]
        else:
            return [doc["sol1"], doc["sol2"]]

    def doc_to_label(self, doc):
        return doc["label"]

    def doc_to_domain_conditional(self, doc):
        del doc
        return "Answer:"


class HellaSwag(ICLMultiChoiceTaskDataset):
    """HellaSwag concats "ACTIVITY_LABEL: CTX_A CTX_B.capitalize()" to form context and then sends endings as continuations
        space added as prefix to each continuation

    {
        'activity_label': 'Roof shingle removal',
        'ctx_a': 'A man is sitting on a roof.',
        'ctx_b': 'he',
        'ctx': 'A man is sitting on a roof. he',
        'endings': ['is using wrap to wrap a pair of skis.', 'is ripping level tiles off.', "is holding a rubik's cube.", 'starts pulling up roofing on a roof.'],
        'label': '3'
    }
    """

    metric_type = "len_norm"

    def __init__(
        self,
        tokenizer,
        dataset_path="hellaswag",
        dataset_name=None,
        sft: bool = False,
        sft_use_label: bool = True,
        model_ctx_len: int = 2048,
        split="validation",
    ):
        super().__init__(
            tokenizer=tokenizer,
            dataset_path=dataset_path,
            dataset_name=dataset_name,
            sft=sft,
            sft_use_label=sft_use_label,
            model_ctx_len=model_ctx_len,
            split=split,
        )

    @classmethod
    def preprocess(cls, text):
        text = text.strip()
        # NOTE: Brackets are artifacts of the WikiHow dataset portion of HellaSwag.
        text = text.replace(" [title]", ". ")
        text = re.sub("\\[.*?\\]", "", text)
        text = text.replace("  ", " ")

        return text

    def doc_to_text(self, doc):
        return self.preprocess(doc["activity_label"] + ": " + doc["ctx_a"] + " " + doc["ctx_b"].capitalize())

    def doc_to_continuations(self, doc):
        if self.add_prefix_space:
            return [" " + self.preprocess(ending) for ending in doc["endings"]]
        else:
            return [self.preprocess(ending) for ending in doc["endings"]]

    def doc_to_label(self, doc):
        return int(doc["label"])

    def doc_to_domain_conditional(self, doc):
        domain_conditional = self.preprocess(doc["ctx_b"].capitalize())

        # ensure non 0 len domain conditional
        if len(domain_conditional) == 0:
            return self.preprocess(doc["ctx_a"]).split(" ")[-1]

        return domain_conditional


class WinoGrande(ICLMultiChoiceTaskDataset):
    """Prompt: split sentence at _ "SENTENCE[:idx] + OPTION1/OPTION2", where idx = SENTENCE.index("_")
        implement PMI_DC
        acc, random at 50%
        continuation is everything in setnence after '_' (" SENTENCE[idx:].strip()")

        Req_loglikelihood('People think Samantha', ' is embarassed, because Samantha made snide comments about the shirt Rebecca was wearing.')
        Req_loglikelihood('People think Rebecca', ' is embarassed, because Samantha made snide comments about the shirt Rebecca was wearing.')

    {
        'sentence': 'People think _ is embarassed, because Samantha made snide comments about the shirt Rebecca was wearing.',
        'option1': 'Samantha',
        'option2': 'Rebecca',
        'answer': '2'
    }

    TODO: might need to write custom metric for Winogrande
    """

    metric_type = "acc"

    def __init__(
        self,
        tokenizer,
        dataset_path="winogrande",
        dataset_name="winogrande_xl",
        sft: bool = False,
        sft_use_label: bool = True,
        model_ctx_len: int = 2048,
        split: str = "validation",
    ):
        # all winogrande datasets have same val set
        super().__init__(
            tokenizer=tokenizer,
            dataset_path=dataset_path,
            dataset_name=dataset_name,
            split=split,
            sft=sft,
            sft_use_label=sft_use_label,
            model_ctx_len=model_ctx_len,
        )

    # def prep_examples(self):
    #     """Overwrite for WinoGrande as multiple ctx, single continuation"""
    #     doc_id = 0
    #     token_count = 0
    #     for doc in tqdm(self.dataset):
    #         if self.sft:
    #             if self.sft_use_label:
    #                 sft_text = self.doc_to_sft(doc)
    #             else:
    #                 sft_text = self.doc_to_sft_without_answer(doc)
    #             sft = self.token_encode(sft_text)
    #             token_count += len(sft)
    #             # form a sample (Hack-y)
    #             self.samples.append({"doc_id": doc_id, "label_id": self.doc_to_label(doc), "sft": sft})

    #         else:
    #             # here ctx is a list
    #             ctxs = self.doc_to_text(doc)
    #             dcs = self.doc_to_domain_conditional(doc)

    #             continuation = self.doc_to_continuations(doc)
    #             label_id = self.doc_to_label(doc)
    #             cont_str_len = len(continuation)  # - 1  # continuations contain leading blank space

    #             # tokenize
    #             continuation = self.token_encode(continuation)

    #             for cont_id, (ctx, dc) in enumerate(zip(ctxs, dcs)):
    #                 ctx = self.token_encode(ctx)
    #                 dc = self.token_encode(dc)

    #                 # query, remove last token from continuation, truncate from left is longer than model ctx length
    #                 query = ctx + continuation  # [:-1]
    #                 query = query[-self.model_ctx_len :]

    #                 # get domain conditional query
    #                 # we don't expect this to be longer than self.model_ctx_len and it won't make sense to truncate from left
    #                 dc_query = dc + continuation  # [:-1]

    #                 # form a sample
    #                 self.samples.append(
    #                     {
    #                         "doc_id": doc_id,
    #                         "cont_id": cont_id,
    #                         "ctx": ctx,
    #                         "continuation": continuation,
    #                         "ctx_len": len(ctx),
    #                         "dc_len": len(dc),
    #                         "cont_len": len(
    #                             continuation
    #                         ),  # even if query has last token removed, LM will output same cont len
    #                         "cont_str_len": cont_str_len,
    #                         "query": query,  # remove last token from continuation
    #                         "dc_query": dc_query,
    #                         "label_id": label_id,
    #                         "sft": None,
    #                     }
    #                 )

    #         doc_id += 1
    #     print(f"Total tokens for SFT: {token_count}")

    # def doc_to_text(self, doc):
    #     # special case where there are multiple ctx and single continuation
    #     pronoun_loc = doc["sentence"].index("_")

    #     ctx = []
    #     for option in [doc["option1"], doc["option2"]]:
    #         ctx.append(doc["sentence"][:pronoun_loc] + option)

    #     return ctx

    # def doc_to_continuations(self, doc):
    #     pronoun_loc = doc["sentence"].index("_") + 1
    #     return doc["sentence"][pronoun_loc:].strip()\

    def doc_to_text(self, doc):
        pronoun_loc = doc["sentence"].index("_")
        return doc["sentence"][: pronoun_loc - 1]

    def doc_to_continuations(self, doc):
        pronoun_loc = doc["sentence"].index("_")
        conclusion = doc["sentence"][pronoun_loc + 1 :]
        if self.add_prefix_space:
            return [" " + doc["option1"] + conclusion, " " + doc["option2"] + conclusion]
        else:
            return [doc["option1"] + conclusion, doc["option2"] + conclusion]

    def doc_to_label(self, doc):
        return int(doc["answer"]) - 1

    def doc_to_domain_conditional(self, doc):
        # """same number of domain conditionals as context"""
        # return [doc["option1"], doc["option2"]]
        return ""

    def doc_to_sft(self, doc):
        ctx = self.doc_to_text(doc)
        sft_example = ctx + self.doc_to_continuations(doc)[self.doc_to_label(doc)]
        return sft_example

    def doc_to_sft_without_answer(self, doc):
        raise NotImplementedError


class OpenBookQA(ICLMultiChoiceTaskDataset):
    """OBQA: question_stem is sent as context (no special prompt format) and choices are sent as continuation
        space added as prefix to each continuation

        implement PMI_DC

    {
        'question_stem': 'Frilled sharks and angler fish live far beneath the surface of the ocean, which is why they are known as',
        'choices': {'text': ['Deep sea animals', 'fish', 'Long Sea Fish', 'Far Sea Animals'],
        'label': ['A', 'B', 'C', 'D']},
        'answerKey': 'A'
    }
    """

    metric_type = "len_norm"

    def __init__(
        self,
        tokenizer,
        dataset_path="openbookqa",
        dataset_name="main",
        sft: bool = False,
        sft_use_label: bool = True,
        model_ctx_len: int = 2048,
        split: str = "validation",
    ):
        super().__init__(
            tokenizer=tokenizer,
            dataset_path=dataset_path,
            dataset_name=dataset_name,
            sft=sft,
            sft_use_label=sft_use_label,
            model_ctx_len=model_ctx_len,
            split=split,
        )

    def doc_to_text(self, doc):
        return doc["question_stem"]

    def doc_to_continuations(self, doc):
        if self.add_prefix_space:
            return [" " + choice for choice in doc["choices"]["text"]]
        else:
            return doc["choices"]["text"]

    def doc_to_label(self, doc):
        return ["A", "B", "C", "D"].index(doc["answerKey"].strip())

    def doc_to_domain_conditional(self, doc):
        return doc["question_stem"].strip().split(" ")[-1]


class BoolQ(ICLMultiChoiceTaskDataset):
    """Prompt: "PASSAGE\nQuestion: QUESTION?\nAnswer:"
    acc, random at 50% (SuperGLUE)
    continuation: yes, no

    {
        'question': 'is ncis new orleans over for the season',
        'passage': 'NCIS: New Orleans (season 4) -- The fourth season of NCIS: New Orleans premiered on September 26, 2017 on CBS. The series continues to air following Bull, Tuesday at 10:00 p.m. (ET) and contained 24 episodes. The season concluded on May 15, 2018.',
        'label': 1
    }
    """

    metric_type = "acc"

    def __init__(
        self,
        tokenizer,
        dataset_path="boolq",
        dataset_name=None,
        sft: bool = False,
        sft_use_label: bool = True,
        model_ctx_len: int = 2048,
        split: str = "validation",
    ):
        super().__init__(
            tokenizer=tokenizer,
            dataset_path=dataset_path,
            dataset_name=dataset_name,
            sft=sft,
            sft_use_label=sft_use_label,
            model_ctx_len=model_ctx_len,
            split=split,
        )

    def doc_to_text(self, doc):
        # return "Passage: " + doc["passage"] + "\nQuestion: " + doc["question"].capitalize() + "?\nAnswer:"
        return (
            "Passage: " + doc["passage"] + "\nQuestion: " + doc["question"].capitalize() + "?\nAnswer (yes orno):"
        )

    def doc_to_continuations(self, doc):
        del doc
        # if self.add_prefix_space:
        #     return [" Yes", " No"]
        # else:
        #     return ["Yes", "No"]
        if self.add_prefix_space:
            return [" yes", " no"]
        else:
            return ["yes", "no"]

    def doc_to_label(self, doc):
        # if doc['answer'] is True, return index of " yes" which is 0
        if doc["answer"]:
            return 0
        else:
            return 1

    def doc_to_domain_conditional(self, doc):
        del doc
        return "Answer:"


class SciQ(ICLMultiChoiceTaskDataset):
    """SciQ sends context as "SUPPORT\nQuestion: QUESTION\nAnswer:" and then distractors + correct_answer as continuations
        space added as prefix to each continuation

        implement PMI_DC

    {
        'question': 'Who proposed the theory of evolution by natural selection?',
        'distractor3': 'Scopes',
        'distractor1': 'Linnaeus',
        'distractor2': 'shaw',
        'correct_answer': 'darwin',
        'support': ''
    }
    """

    metric_type = "acc"

    def __init__(
        self,
        tokenizer,
        dataset_path="sciq",
        dataset_name=None,
        sft: bool = False,
        sft_use_label: bool = True,
        model_ctx_len: int = 2048,
        split: str = "validation",
    ):
        super().__init__(
            tokenizer=tokenizer,
            dataset_path=dataset_path,
            dataset_name=dataset_name,
            sft=sft,
            sft_use_label=sft_use_label,
            model_ctx_len=model_ctx_len,
            split=split,
        )

    def doc_to_text(self, doc):
        return doc["support"].strip() + "\nQuestion: " + doc["question"] + "\nAnswer:"

    def doc_to_continuations(self, doc):
        if self.add_prefix_space:
            return [
                " " + doc["distractor1"],
                " " + doc["distractor2"],
                " " + doc["distractor3"],
                " " + doc["correct_answer"],
            ]
        else:
            return [
                doc["distractor1"],
                doc["distractor2"],
                doc["distractor3"],
                doc["correct_answer"],
            ]

    def doc_to_label(self, doc):
        del doc
        return 3

    def doc_to_domain_conditional(self, doc):
        del doc
        return "Answer:"


class ArcEasy(ICLMultiChoiceTaskDataset):
    """ArcEasy creates context with "Question: QUESTION\nAnswer:" and sends the choices as continuations
        space added as prefix to each continuation

    {
        'question': 'Which technology was developed most recently?',
        'choices': {'text': ['cellular telephone', 'television', 'refrigerator', 'airplane'],
        'label': ['A', 'B', 'C', 'D']},
        'answerKey': 'A'
    }
    """

    metric_type = "acc"

    def __init__(
        self,
        tokenizer,
        dataset_path="ai2_arc",
        dataset_name="ARC-Easy",
        sft: bool = False,
        sft_use_label: bool = True,
        model_ctx_len: int = 2048,
        split: str = "validation",
    ):
        super().__init__(
            tokenizer=tokenizer,
            dataset_path=dataset_path,
            dataset_name=dataset_name,
            sft=sft,
            sft_use_label=sft_use_label,
            model_ctx_len=model_ctx_len,
            split=split,
        )

    def doc_to_text(self, doc):
        return "Question: " + doc["question"] + "\nAnswer:"

    def doc_to_continuations(self, doc):
        if self.add_prefix_space:
            return [" " + choice for choice in doc["choices"]["text"]]
        else:
            return doc["choices"]["text"]

    def doc_to_label(self, doc):
        # some doc["answerKey"] are stored as numbers
        num_to_letter = {"1": "A", "2": "B", "3": "C", "4": "D", "5": "E"}

        if doc["answerKey"] in num_to_letter:
            doc["answerKey"] = num_to_letter[doc["answerKey"]]

        return ["A", "B", "C", "D", "E"].index(doc["answerKey"])

    def doc_to_domain_conditional(self, doc):
        del doc
        return "Answer:"


class ArcChallenge(ArcEasy):
    """ArcChallenge follows the same prompt format as ArcEasy.
    implement PMI_DC
    """

    metric_type = "len_norm"  # Ideally "pmi_dc"

    def __init__(
        self,
        tokenizer,
        dataset_path="ai2_arc",
        dataset_name="ARC-Challenge",
        sft: bool = False,
        sft_use_label: bool = True,
        model_ctx_len: int = 2048,
        split: str = "validation",
    ):
        super().__init__(
            tokenizer=tokenizer,
            dataset_path=dataset_path,
            dataset_name=dataset_name,
            split=split,
            sft=sft,
            sft_use_label=sft_use_label,
            model_ctx_len=model_ctx_len,
        )


class ArcEasyCELoss(ArcEasy):
    """ArcEasyCELoss is ARCEasy using an alternate ce_loss metric"""

    metric_type = "ce_loss"

    def doc_to_continuations(self, doc):
        # We only consider the correct answer for this metric
        answer = doc["choices"]["text"][self.doc_to_label(doc)]
        return [" " + answer]

    def doc_to_label(self, doc):
        return 0


class BasicArithmetic(ArcEasy):
    """This is a basic arithmetic task follows the same prompt format as ArcEasy.
    Example:
    {"id": "q85_1d1d_max1d_plus",
    "question": "Calculate 2 + 5 =",
    "choices": {"text": ["8", "7", "6", "17"],
    "label": ["A", "B", "C", "D"]},
    "answerKey": "B", "type_tag": "easy"}

    """

    metric_type = "acc"

    def __init__(
        self,
        tokenizer,
        dataset_path="allenai/basic_arithmetic",
        dataset_name=None,
        sft: bool = False,
        sft_use_label: bool = True,
        split: str = "validation",
    ):
        super().__init__(tokenizer=tokenizer, dataset_path=dataset_path, dataset_name=dataset_name, split=split)


class CommonsenseQA(ArcEasy):
    """CommonsenseQA
    Example:
    {'id': 'e68fb2448fd74e402aae9982aa76e527',
    'question': 'Where are  you likely to find a hamburger?',
    'question_concept': 'hamburger',
    'choices': {'label': ['A', 'B', 'C', 'D', 'E'],
    'text': ['fast food restaurant', 'pizza', 'ground up dead cows', 'mouth', 'cow carcus']},
    'answerKey': 'A'}
    """

    metric_type = "len_norm"

    def __init__(
        self,
        tokenizer,
        dataset_path="tau/commonsense_qa",
        dataset_name=None,
        sft: bool = False,
        sft_use_label: bool = True,
        split: str = "validation",
    ):
        super().__init__(
            tokenizer=tokenizer,
            dataset_path=dataset_path,
            dataset_name=dataset_name,
            split=split,
        )


class SocialIQa(ICLMultiChoiceTaskDataset):
    """SocialIQa
    Example:
    {'context': 'Jordan was in charge of taking the food on the camping trip and left all the food at home.',
     'question': 'How would Jordan feel afterwards?',
     'answerA': 'horrible that he let his friends down on the camping trip',
     'answerB': "happy that he doesn't need to do the cooking on the trip",
     'answerC': 'very proud and accomplished about the camping trip', 'label': '1'}
    """

    metric_type = "len_norm"

    def __init__(
        self,
        tokenizer,
        dataset_path="social_i_qa",
        dataset_name=None,
        sft: bool = False,
        sft_use_label: bool = True,
        split: str = "validation",
    ):
        super().__init__(
            tokenizer=tokenizer,
            dataset_path=dataset_path,
            dataset_name=dataset_name,
            split=split,
        )

    def doc_to_text(self, doc):
        return "Question: " + doc["context"] + " " + doc["question"] + "\nAnswer:"

    def doc_to_continuations(self, doc):
        if self.add_prefix_space:
            return [" " + doc["answerA"], " " + doc["answerB"], " " + doc["answerC"]]
        else:
            return [doc["answerA"], doc["answerB"], doc["answerC"]]

    def doc_to_label(self, doc):
        return int(doc["label"]) - 1

    def doc_to_domain_conditional(self, doc):
        return "Answer:"


class COPA(ICLMultiChoiceTaskDataset):
    """Prompt: "PREMISE.strip()[:-1] because/therefore"
    Req_loglikelihood('The pair of students came under scrutiny by the teacher because', ' the students both received excellent grades.'
    continuations: CHOICE1/CHOICE2

    "cause": "because",
    "effect": "therefore",

    implement PMI_DC
    acc, random at 50%

    {
        'premise': 'The pair of students came under scrutiny by the teacher.',
        'choice1': 'The students both received excellent grades.',
        'choice2': 'Their responses on the assignment were identical.',
        'question': 'cause',
        'label': 1
    }
    """

    metric_type = "acc"

    def __init__(
        self,
        tokenizer,
        dataset_path="super_glue",
        dataset_name="copa",
        sft: bool = False,
        sft_use_label: bool = True,
        model_ctx_len: int = 2048,
        split: str = "validation",
    ):
        super().__init__(
            tokenizer=tokenizer,
            dataset_path=dataset_path,
            dataset_name=dataset_name,
            sft=sft,
            sft_use_label=sft_use_label,
            model_ctx_len=model_ctx_len,
            split=split,
        )

    def doc_to_text(self, doc):
        connector = "because" if doc["question"] == "cause" else "therefore"

        # remove the period
        return doc["premise"].strip()[:-1] + " " + connector

    def doc_to_continuations(self, doc):
        def convert_choice(choice):
            return choice[0].lower() + choice[1:]

        if self.add_prefix_space:
            return [" " + convert_choice(doc["choice1"]), " " + convert_choice(doc["choice2"])]
        else:
            return [convert_choice(doc["choice1"]), convert_choice(doc["choice2"])]

    def doc_to_label(self, doc):
        return doc["label"]

    def doc_to_domain_conditional(self, doc):
        return "because" if doc["question"] == "cause" else "therefore"


class RTE(ICLMultiChoiceTaskDataset):
    """Prompt: "SENTENCE1\nQuestion: SENTENCE2 True or False?\nAnswer:"
    implement PMI_DC
    acc, random at 50% (GLUE)
    continuations: True, False

    {
        'sentence1': 'The number of Danes opposed to swapping the krone for the euro has increased slightly to 35.3 percent, up from 34.6 percent in April, according to a poll published on Thursday by Danske Bank.',
        'sentence2': 'The introduction of the euro has been opposed.',
        'label': 0,
    }
    """

    metric_type = "len_norm"

    def __init__(
        self, tokenizer, dataset_path="glue", dataset_name="rte", sft: bool = False, sft_use_label: bool = True
    ):
        super().__init__(
            tokenizer=tokenizer,
            dataset_path=dataset_path,
            dataset_name=dataset_name,
        )

    def doc_to_text(self, doc):
        return doc["sentence1"] + "\nQuestion: " + doc["sentence2"] + " True or False?\nAnswer:"

    def doc_to_continuations(self, doc):
        del doc
        if self.add_prefix_space:
            return [" True", " False"]
        else:
            return ["True", "False"]

    def doc_to_label(self, doc):
        return doc["label"]

    def doc_to_domain_conditional(self, doc):
        del doc
        return "Answer:"


class CommitmentBank(ICLMultiChoiceTaskDataset):
    """Prompt: "PREMISE\nQuestion: HYPOTHESIS. True, False or Neither?\nAnswer:"
    continuations: True, False, Neither

        implement PMI_DC
        acc/F1, random at 33% acc. (SuperGLUE)

    {
        'premise': 'Then they would awake, terrified and sweating, to find themselves in white starched linen, in a comfortable bed, in peaceful England. And all would be well. It may be said that although he survived it the siege nevertheless had a bad effect on the Collector.',
        'hypothesis': 'the siege nevertheless had a bad effect on the Collector',
        'label': 0
    }
    """

    metric_type = "acc"

    def __init__(
        self,
        tokenizer,
        dataset_path="super_glue",
        dataset_name="cb",
        sft: bool = False,
        sft_use_label: bool = True,
        split: str = "validation",
    ):
        super().__init__(tokenizer=tokenizer, dataset_path=dataset_path, dataset_name=dataset_name, split=split)

    def doc_to_text(self, doc):
        return doc["premise"] + "\nQuestion: " + doc["hypothesis"] + ". True, False or Neither?\nAnswer:"

    def doc_to_continuations(self, doc):
        del doc
        if self.add_prefix_space:
            return [" True", " False", " Neither"]
        else:
            return ["True", "False", "Neither"]

    def doc_to_label(self, doc):
        return doc["label"]

    def doc_to_domain_conditional(self, doc):
        del doc
        return "Answer:"


class MRPC(ICLMultiChoiceTaskDataset):
    """Prompt for MRPC is formed using "Sentence 1: SENTENCE1\nSentence 2: SENTENCE2\nQuestion: Do both sentences mean the same thing?\nAnswer:"
    acc/F1, random at 50% acc. (GLUE)
    continuations: yes and no

    {
        'sentence1': 'In fiction : Edward P. Jones ( " The Known World " ) and Scott Spencer ( " A Ship Made of Paper " ) .',
        'sentence2': 'The fifth nominee for fiction is Scott Spencer , for A Ship Made of Paper .',
        'label': 0
    }
    """

    metric_type = "f1"

    def __init__(
        self,
        tokenizer,
        dataset_path="glue",
        dataset_name="mrpc",
        sft: bool = False,
        sft_use_label: bool = True,
        split: str = "validation",
    ):
        super().__init__(tokenizer=tokenizer, dataset_path=dataset_path, dataset_name=dataset_name, split=split)

    @classmethod
    def preprocess(cls, string: str) -> str:
        string = string.replace(" n't", "n't")
        string = string.replace(" )", ")")
        string = string.replace("( ", "(")
        string = string.replace('" ', '"')
        string = string.replace(' "', '"')

        string = re.sub(r" (['.,])", r"\1", string)

        return string

    def doc_to_text(self, doc):
        return (
            "Sentence 1: "
            + self.preprocess(doc["sentence1"])
            + "\nSentence 2: "
            + self.preprocess(doc["sentence2"])
            + "\nQuestion: Do both sentences mean the same thing?\nAnswer:"
        )

    def doc_to_continuations(self, doc):
        del doc
        if self.add_prefix_space:
            return [" yes", " no"]
        else:
            return ["yes", "no"]

    def doc_to_label(self, doc):
        # if doc['label'] is True, return index of " yes" which is 0
        if doc["label"]:
            return 0
        else:
            return 1

    def doc_to_domain_conditional(self, doc):
        del doc
        return "Answer:"


class SST2(ICLMultiChoiceTaskDataset):
    """SST2 task formats prompts as "SENTENCE\nQuestion: Is this sentence positive or negative?\nAnswer:"
    some preprocessing done on sentence

    constructs 2 requests, 1 for positive and another for negative
    positive and negative have just 1 token in tokenizer
    positive: 1313
    negative: 2430

    implement PMI_DC
    acc, random at 50% (GLUE)

    {
        'sentence': "harrison 's flowers puts its heart in the right place , but its brains are in no particular place at all . ",
        'label': 1,
    }
    """

    metric_type = "acc"

    def __init__(
        self, tokenizer, dataset_path="glue", dataset_name="sst2", sft: bool = False, sft_use_label: bool = True
    ):
        super().__init__(
            tokenizer=tokenizer,
            dataset_path=dataset_path,
            dataset_name=dataset_name,
        )

    @classmethod
    def preprocess(cls, string: str) -> str:
        string = string.replace(" n't", "n't")
        string = string.replace(" )", ")")
        string = string.replace("( ", "(")
        string = string.replace('" ', '"')
        string = string.replace(' "', '"')

        string = re.sub(r" (['.,])", r"\1", string)

        return string

    def doc_to_text(self, doc):
        return self.preprocess(doc["sentence"]) + "\nQuestion: Is this sentence positive or negative?\nAnswer:"

    def doc_to_continuations(self, doc):
        del doc
        # {1: "positive", 0: "negative"}
        if self.add_prefix_space:
            return [" negative", " positive"]
        else:
            return ["negative", "positive"]

    def doc_to_label(self, doc):
        # {1: "positive", 0: "negative"}
        return doc["label"]

    def doc_to_domain_conditional(self, doc):
        del doc
        return "Answer:"


class MMLU(ICLMultiChoiceTaskDataset):
    """MMLU creates context with "Question: QUESTION\nAnswer:" and sends the choices as continuations
           space added as prefix to each continuation

       {
           'question': "Which of the following terms describes the body's ability to maintain its normal state?",
           'subject': 'anatomy',
           'choices': ['Anabolism', 'Catabolism', 'Tolerance', 'Homeostasis'],
    '       answer': 3
        }
    """

    metric_type = "len_norm"  # Ideally pmi_dc

    _subcategories = {
        "abstract_algebra": ["math"],
        "anatomy": ["health"],
        "astronomy": ["physics"],
        "business_ethics": ["business"],
        "clinical_knowledge": ["health"],
        "college_biology": ["biology"],
        "college_chemistry": ["chemistry"],
        "college_computer_science": ["computer science"],
        "college_mathematics": ["math"],
        "college_medicine": ["health"],
        "college_physics": ["physics"],
        "computer_security": ["computer science"],
        "conceptual_physics": ["physics"],
        "econometrics": ["economics"],
        "electrical_engineering": ["engineering"],
        "elementary_mathematics": ["math"],
        "formal_logic": ["philosophy"],
        "global_facts": ["other"],
        "high_school_biology": ["biology"],
        "high_school_chemistry": ["chemistry"],
        "high_school_computer_science": ["computer science"],
        "high_school_european_history": ["history"],
        "high_school_geography": ["geography"],
        "high_school_government_and_politics": ["politics"],
        "high_school_macroeconomics": ["economics"],
        "high_school_mathematics": ["math"],
        "high_school_microeconomics": ["economics"],
        "high_school_physics": ["physics"],
        "high_school_psychology": ["psychology"],
        "high_school_statistics": ["math"],
        "high_school_us_history": ["history"],
        "high_school_world_history": ["history"],
        "human_aging": ["health"],
        "human_sexuality": ["culture"],
        "international_law": ["law"],
        "jurisprudence": ["law"],
        "logical_fallacies": ["philosophy"],
        "machine_learning": ["computer science"],
        "management": ["business"],
        "marketing": ["business"],
        "medical_genetics": ["health"],
        "miscellaneous": ["other"],
        "moral_disputes": ["philosophy"],
        "moral_scenarios": ["philosophy"],
        "nutrition": ["health"],
        "philosophy": ["philosophy"],
        "prehistory": ["history"],
        "professional_accounting": ["other"],
        "professional_law": ["law"],
        "professional_medicine": ["health"],
        "professional_psychology": ["psychology"],
        "public_relations": ["politics"],
        "security_studies": ["politics"],
        "sociology": ["culture"],
        "us_foreign_policy": ["politics"],
        "virology": ["health"],
        "world_religions": ["philosophy"],
        "auxiliary_train": ["auxiliary_train"],
    }

    _categories = {
        "stem": ["physics", "chemistry", "biology", "computer science", "math", "engineering"],
        "humanities": ["history", "philosophy", "law"],
        "social_sciences": ["politics", "culture", "economics", "geography", "psychology"],
        "other": ["other", "business", "health"],
        "auxiliary_train": ["auxiliary_train"],
    }

    def __init__(
        self,
        tokenizer,
        dataset_path="hails/mmlu_no_train",
        dataset_name=None,
        split="validation",
        prompt_variations=None,
        mc_labels=False,
        sft=False,
        sft_use_label=True,
        model_ctx_len=2048,
    ):
        dataset_names = []
        # Collect the relevant categories
        if dataset_name in MMLU._categories:
            for sub_cat in MMLU._categories[dataset_name]:
                for name, cats in MMLU._subcategories.items():
                    if sub_cat in cats:
                        dataset_names.append(name)
        elif dataset_name in MMLU._subcategories:
            dataset_names.append(dataset_name)
        else:  # E.g., "math"
            for name, cats in MMLU._subcategories.items():
                if dataset_name in cats:
                    dataset_names.append(name)
        self.dev_set = {}
        self.mc_labels = mc_labels
        prompts: List[Union[None, str]] = [None]
        if prompt_variations is not None:
            if prompt_variations == 1:
                prompts = [None, "inst", "inst+1", "inst+2", "inst+3", "inst+4", "inst+5"]
            elif prompt_variations == 2:
                prompts = ["inst+5"]
            else:
                raise ValueError(f"Unknown prompt variations: {prompt_variations}")
            # Need to grab the dev set for the few-shot prompts
            for name in dataset_names:
                self.dev_set[name] = datasets.load_dataset(
                    path=dataset_path, name=name, split="dev", trust_remote_code=True
                )
        super().__init__(
            tokenizer=tokenizer,
            dataset_path=dataset_path,
            dataset_name=dataset_names,
            split=split,
            prompts=prompts,
            sft=sft,
            sft_use_label=sft_use_label,
            model_ctx_len=model_ctx_len,
        )

    def doc_to_text(self, doc):
        def format_example(doc, keys):
            question_prefix = ""
            if not self.mc_labels:
                question_prefix = "Question: "  # To make context more clear
            question = question_prefix + doc["question"].strip()
            choices = ""
            if self.mc_labels:
                choices = "".join([f"{key}. {choice}\n" for key, choice in zip(keys, doc["choices"])])
            prompt = f"{question}\n{choices}Answer:"
            return prompt

        keys = ["A", "B", "C", "D"]
        output_text = format_example(doc, keys)

        if self.current_prompt is not None:
            prefix = ""
            if "inst" in self.current_prompt:
                subject = doc.get("subject").replace("_", " ")
                prefix = f"The following are multiple choice questions (with answers) about {subject}:\n\n"
            num_shots = re.findall("\\+(\\d+)", self.current_prompt)
            if num_shots:
                dev_set = self.dev_set.get(doc.get("subject"), [])
                num_shots_int = int(num_shots[0])
                for idx, dev_doc in enumerate(dev_set):
                    if idx >= num_shots_int:
                        break
                    if self.mc_labels:
                        answer = keys[dev_doc["answer"]]
                    else:
                        answer = dev_doc["choices"][dev_doc["answer"]]
                    prefix += format_example(dev_doc, keys) + " " + answer + "\n\n"
            output_text = prefix + output_text
        return output_text

    def doc_to_continuations(self, doc):
        if self.mc_labels:
            if self.add_prefix_space:
                return [" A", " B", " C", " D"]
            else:
                return ["A", "B", "C", "D"]

        if self.add_prefix_space:
            return [" " + choice for choice in doc["choices"]]
        else:
            return doc["choices"]

    def doc_to_label(self, doc):
        return doc["answer"]

    def doc_to_domain_conditional(self, doc):
        del doc
        return "Answer:"

    def doc_to_sft(self, doc):
        sft_example = self.doc_to_text(doc) + self.doc_to_continuations(doc)[self.doc_to_label(doc)]
        return sft_example

    def doc_to_sft_without_answer(self, doc):
        return self.doc_to_text(doc)


class CodeComprehension(ICLMultiChoiceTaskDataset):
    """imbue/code-comprehension
    Example:
    {"question": "If we execute the code below, what will `result` be equal to? ```python l = 'likewise' match = None for k in ['margin', 'rachel', 'convinced', 'sociology']: if k in 'margi': match = k break else: match = 'robbie' result = match ```",
     "choices": [ "'robbie'", "'sociolog'", "'likewise'", "'convince'" ],
     "correct_answer": 'robbie'}
    """

    metric_type = "len_norm"

    def __init__(
        self,
        tokenizer,
        dataset_path="imbue/code-comprehension",
        dataset_name=None,
        sft: bool = False,
        sft_use_label: bool = True,
        split: str = "train",
        model_ctx_len: int = 2048,
    ):
        super().__init__(
            tokenizer=tokenizer,
            dataset_path=dataset_path,
            dataset_name=dataset_name,
            split=split,
            sft=sft,
            sft_use_label=sft_use_label,
            model_ctx_len=model_ctx_len,
        )

    def doc_to_text(self, doc):
        return "Question:\n" + doc["question"] + "\nAnswer:"

    def doc_to_continuations(self, doc):
        if self.add_prefix_space:
            return [" " + d for d in doc["choices"]]
        else:
            return doc["choices"]

    def doc_to_label(self, doc):
        return doc["choices"].index(doc["correct_answer"])

    def doc_to_domain_conditional(self, doc):
        return "Answer:"


class Imbue(ICLMultiChoiceTaskDataset):
    """imbue/high_quality_public_evaluations or high_quality_private_evaluations
    Example:
    {"question": "If we execute the code below, what will `result` be equal to? ```python l = 'likewise' match = None for k in ['margin', 'rachel', 'convinced', 'sociology']: if k in 'margi': match = k break else: match = 'robbie' result = match ```",
     "choices": [ "'robbie'", "'sociolog'", "'likewise'", "'convince'" ],
     "correct_answer": 'robbie'}
    """

    metric_type = "len_norm"

    def __init__(
        self,
        tokenizer,
        dataset_path="imbue/high_quality_public_evaluations",
        dataset_name=None,
        task="all",  # all, arc, codegen, winogrande, boolq, ...
        sft: bool = False,
        sft_use_label: bool = True,
        split: str = "train",
        model_ctx_len: int = 2048,
    ):
        if task == "all":
            filter_fn = None
        else:
            filter_fn = lambda example: example["original_dataset"] == task
        super().__init__(
            tokenizer=tokenizer,
            dataset_path=dataset_path,
            dataset_name=dataset_name,
            split=split,
            sft=sft,
            sft_use_label=sft_use_label,
            model_ctx_len=model_ctx_len,
            filter_fn=filter_fn,
        )

    def doc_to_text(self, doc):
        return "Question:\n" + doc["question"] + "\nAnswer:"

    def doc_to_continuations(self, doc):
        if self.add_prefix_space:
            return [" " + d for d in doc["choices"]]
        else:
            return doc["choices"]

    def doc_to_label(self, doc):
        return doc["choices"].index(doc["correct_choices"][0])

    def doc_to_domain_conditional(self, doc):
        return "Answer:"


def CE_loss_wrapper(icl_dataset):
    """Wrap a task with a CE loss metric"""

    class CE_loss_Dataset(icl_dataset):
        metric_type = "ce_loss"

        def doc_to_continuations(self, doc):
            conts = super().doc_to_continuations(doc)
            label = super().doc_to_label(doc)
            return [conts[label]]

        def doc_to_label(self, doc):
            return 0

    return CE_loss_Dataset


def Context_CE_loss_wrapper(icl_dataset):
    """Wrap a task with a CE loss metric"""

    class CE_loss_Dataset(icl_dataset):
        metric_type = "ctx_ce_loss"

        def doc_to_continuations(self, doc):
            conts = super().doc_to_continuations(doc)
            label = super().doc_to_label(doc)
            return [conts[label]]

        def doc_to_label(self, doc):
            return 0

    return CE_loss_Dataset


label_to_task_map = {
    # New tasks
    "imbue_code_comprehension": CodeComprehension,  # 54k
    "imbue_all": Imbue,  # 13k
    "imbue_gsm8k": (Imbue, {"task": "gsm8k"}),  # 1k
    "imbue_codegen": (Imbue, {"task": "codegen"}),  # 1k
    "imbue_arc": (Imbue, {"task": "arc"}),  # 1k
    "imbue_wic": (Imbue, {"task": "wic"}),  # 1k
    "imbue_winogrande": (Imbue, {"task": "winogrande"}),  # 1k
    "imbue_anli": (Imbue, {"task": "anli"}),  # 1k
    # "imbue_multirc": (Imbue, {"task": "multirc"}),  # 1k # multiple answers
    "imbue_openbookqa": (Imbue, {"task": "openbookqa"}),  # 1k
    "imbue_socialiqa": (Imbue, {"task": "socialiqa"}),  # 1k
    "imbue_copa": (Imbue, {"task": "copa"}),  # 1k
    "imbue_ethics": (Imbue, {"task": "ethics"}),  # 1k
    "imbue_boolq": (Imbue, {"task": "boolq"}),  # 1k
    "imbue_race": (Imbue, {"task": "race"}),  # 1k
    "imbue_hellaswag": (Imbue, {"task": "hellaswag"}),  # 1k
    # Old tasks
    "piqa": PIQA,  # 2k
    "piqa_train": (PIQA, {"split": "train"}),  # 16k
    "piqa_test": PIQA,  # 3k, note no access to real test labels
    "hellaswag": HellaSwag,  # 10k
    "hellaswag_train": (HellaSwag, {"split": "train"}),  # 40k
    "hellaswag_test": HellaSwag,  # 10k, note no access to real test labels
    "winogrande": WinoGrande,  # 1k
    "winogrande_train": (WinoGrande, {"split": "train"}),  # 40k
    "winogrande_test": WinoGrande,  # 2k, note no access to real test labels
    "openbook_qa": OpenBookQA,  # 500
    "openbook_qa_train": (OpenBookQA, {"split": "train"}),  # 5k
    "openbook_qa_test": (OpenBookQA, {"split": "test"}),  # 500
    "boolq": BoolQ,  # 3k
    "boolq_test": BoolQ,  # 3k
    "boolq_train": (BoolQ, {"split": "train"}),  # 10k
    "sciq": SciQ,  # 1k
    "sciq_train": (SciQ, {"split": "train"}),  # 12k
    "sciq_test": (SciQ, {"split": "test"}),  # 1k
    "arc_easy": ArcEasy,  # 500
    "arc_easy_train": (ArcEasy, {"split": "train"}),  # 3k
    "arc_easy_test": (ArcEasy, {"split": "test"}),  # 2k
    "arc_challenge": ArcChallenge,  # 300
    "arc_challenge_train": (ArcChallenge, {"split": "train"}),  # 1k
    "arc_challenge_test": (ArcChallenge, {"split": "test"}),  # 1k
    # Not using things below here
    "basic_arithmetic": BasicArithmetic,
    "copa": COPA,
    "copa_train": (COPA, {"split": "train"}),
    "copa_test": (COPA, {"split": "test"}),
    "rte": RTE,
    "commitment_bank": CommitmentBank,
    "mrpc": MRPC,
    "sst2": SST2,
    "commonsense_qa": CommonsenseQA,
    "social_iqa": SocialIQa,
    "mmlu_auxiliary_train": (
        MMLU,
        {"dataset_path": "cais/mmlu", "dataset_name": "auxiliary_train", "split": "train"},
    ),
    "mmlu_stem_test": (MMLU, {"dataset_name": "stem", "split": "test"}),
    "mmlu_humanities_test": (MMLU, {"dataset_name": "humanities", "split": "test"}),
    "mmlu_social_sciences_test": (MMLU, {"dataset_name": "social_sciences", "split": "test"}),
    "mmlu_other_test": (MMLU, {"dataset_name": "other", "split": "test"}),
    "mmlu_stem": (MMLU, {"dataset_name": "stem"}),
    "mmlu_humanities": (MMLU, {"dataset_name": "humanities"}),
    "mmlu_social_sciences": (MMLU, {"dataset_name": "social_sciences"}),
    "mmlu_other": (MMLU, {"dataset_name": "other"}),
    "mmlu_stem_var": (MMLU, {"dataset_name": "stem", "prompt_variations": 1}),
    "mmlu_humanities_var": (MMLU, {"dataset_name": "humanities", "prompt_variations": 1}),
    "mmlu_social_sciences_var": (MMLU, {"dataset_name": "social_sciences", "prompt_variations": 1}),
    "mmlu_other_var": (MMLU, {"dataset_name": "other", "prompt_variations": 1}),
    "mmlu_stem_mc_5shot": (MMLU, {"dataset_name": "stem", "prompt_variations": 2, "mc_labels": True}),
    "mmlu_humanities_mc_5shot": (MMLU, {"dataset_name": "humanities", "prompt_variations": 2, "mc_labels": True}),
    "mmlu_social_sciences_mc_5shot": (
        MMLU,
        {"dataset_name": "social_sciences", "prompt_variations": 2, "mc_labels": True},
    ),
    "mmlu_other_mc_5shot": (MMLU, {"dataset_name": "other", "prompt_variations": 2, "mc_labels": True}),
    "mmlu_stem_mc_5shot_test": (
        MMLU,
        {"dataset_name": "stem", "split": "test", "prompt_variations": 2, "mc_labels": True},
    ),
    "mmlu_humanities_mc_5shot_test": (
        MMLU,
        {"dataset_name": "humanities", "split": "test", "prompt_variations": 2, "mc_labels": True},
    ),
    "mmlu_social_sciences_mc_5shot_test": (
        MMLU,
        {"dataset_name": "social_sciences", "split": "test", "prompt_variations": 2, "mc_labels": True},
    ),
    "mmlu_other_mc_5shot_test": (
        MMLU,
        {"dataset_name": "other", "split": "test", "prompt_variations": 2, "mc_labels": True},
    ),
}
