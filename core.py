"""Backward-compatible public API for the modular OnCall Memory package."""
from oncall_memory.config import *
from oncall_memory.parsing import memory_text, memory_field, recalled_memory, deduplicate_incidents, recalled_mttr_label, parse_feedback, parse_supersession, associate_feedback, associate_supersessions, rank_feedback_candidates, supersession_badge_text
from oncall_memory.memory import get_hindsight_client, recall_incidents, _recall_with_timeout, reflect_now, build_incident_document
from oncall_memory.llm import GroqCompletionError, groq_error_details, is_groq_timeout, is_reasoning_effort_rejection, is_transient_groq_error, complete_with_model, groq_completion
from oncall_memory.prompts import build_memory_context, ask_groq, answer_memory_question, compress_reflection, no_memory_fallback, recalled_memory_fallback
from oncall_memory.validation import _memory_answer_fallback, validate_memory_answer, validate_memory_question_answer
from oncall_memory.diagnose import diagnose_without_memory, diagnose_with_memory, ask_memory, compute_recurring_insights, validate_supersession_suggestion, suggest_supersession

__all__ = [name for name in globals() if not name.startswith("__")]
