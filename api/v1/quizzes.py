"""Quiz API endpoints with content negotiation (HTML/JSON)."""

import uuid
import asyncio
import logging
from datetime import datetime, timezone, date, timedelta
from typing import Dict, List, Optional, Union, Awaitable, Any, Tuple

from fastapi import APIRouter, Request, Depends, UploadFile, File, HTTPException, Query
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

try:
    import httpx
except ImportError:
    httpx = None

from api.config import settings
from api.dependencies import get_storage, get_ai, get_sessions, get_quiz_contexts, get_user_language, language_preferences, get_analytics_cache
from api.i18n import load_translations
from src.storage import QuizStorage
from src.quiz_ai import QuizAI
from src.parser import QuizParser
from src.models import Question, Choice, Quiz
from src.agents.schemas import QuizContext, LearningProfile, ResponseEvaluation, ErrorType, ErrorEvaluation, Feedback

router = APIRouter()
templates = Jinja2Templates(directory=str(settings.TEMPLATES_DIR))

# ============================================
# Helper Functions
# ============================================
# Serialization and utility functions for API responses

def accepts_json(request: Request) -> bool:
    """Check if client accepts JSON response based on Accept header."""
    accept = request.headers.get("accept", "")
    return "application/json" in accept


def get_template_context(request: Request, **kwargs) -> dict:
    """Get common template context including analytics."""
    lang = get_user_language(request)
    translations = load_translations(lang)
    return {
        "request": request,
        "api_base_url": settings.API_BASE_URL,
        "lang": lang,
        "t": translations,
        "plausible_domain": settings.PLAUSIBLE_DOMAIN,
        **kwargs
    }


def serialize_quizzes(
    quizzes: List[Quiz], 
    quiz_contexts: Optional[Dict[str, Any]] = None
) -> List[Dict]:
    """Serialize quiz objects to API response format.
    
    Returns a list of quiz summaries with slug, topic, question count, time limit, and context status.
    """
    contexts = quiz_contexts or {}
    return [
        {
            "slug": q.slug,
            "topic": q.topic,
            "questions": q.total_questions,
            "time_limit": q.time_limit,
            "is_context_cached": q.slug in contexts
        }
        for q in quizzes
    ]


def serialize_questions(questions: List[Question]) -> List[Dict]:
    """Serialize question objects to API response format.
    
    Returns a list of questions with text, choices, and multiple-choice flag.
    Note: Correct answers are not included in the serialized output.
    """
    return [
        {
            "text": q.text,
            "choices": [c.text for c in q.choices],
            "multiple": len(q.correct_choices) > 1
        }
        for q in questions
    ]


def serialize_response(
    questions: List[Question],
    answers: Optional[Dict[int, List[Choice]]] = None,
    evaluations: Optional[Dict[int, ResponseEvaluation]] = None
) -> Dict[int, Dict[str, Any]]:
    """Serialize user responses and evaluations for answered questions only.
    
    Args:
        questions: List of all quiz questions (for validation).
        answers: Optional dict mapping question index to selected choices.
        evaluations: Optional dict mapping question index to evaluation results.
    
    Returns:
        Dict mapping question index to response data containing:
        - correct: Boolean indicating if answer is correct
        - your_answer: List of selected choice texts
        - correct_answers: List of correct choice texts
        - feedback: Optional feedback object (if evaluation available)
        - suggestions: Optional learning suggestions (if evaluation available)
    """
    response = {}
    answered_idxs = set((answers or {}).keys()) | set((evaluations or {}).keys())
    
    for i in answered_idxs:
        q = questions[i]
        response_data = {}
        
        # Add answer data if available
        if answers and i in answers:
            selected = answers[i]
            response_data["correct"] = set(selected) == set(q.correct_choices)
            response_data["your_answer"] = [c.text for c in selected]
            response_data["correct_answers"] = [c.text for c in q.correct_choices]
        
        # Add evaluation data if available
        if evaluations and i in evaluations:
            evaluation: ResponseEvaluation = evaluations[i]
            if evaluation.feedback:
                response_data["feedback"] = evaluation.feedback.to_dict()
            if evaluation.suggestions:
                response_data["suggestions"] = [s.to_dict() for s in evaluation.suggestions]
        
        response[i] = response_data
    
    return response

# ============================================
# API Endpoints
# ============================================

# ============================================
# User Preferences
# ============================================
# Endpoints for managing user preferences:
# - Set preferred language for UI translations

@router.post("/language")
async def set_language(
    request: Request,
    lang: str = Query(...)
):
    """Set user's preferred language.
    
    Args:
        lang: Language code. Supported languages:
            - en: English
            - hu: Hungarian
            - de: German
            - id: Indonesian (Bahasa)
            - zh: Mandarin Chinese
            - hi: Hindi
            - es: Spanish
            - fr: French
            - ar: Arabic
            - ru: Russian
            - ko: Korean
            - ja: Japanese
            - it: Italian
            - rm: Romansh
            - ur: Urdu
            - bn: Bengali
            - th: Thai
            - lo: Lao
            - mn: Mongolian
    """
    valid_languages = ["en", "hu", "de", "id", "zh", "hi", "es", "fr", "ar", "ru", "ko", "ja", "it", "rm", "ur", "bn", "th", "lo", "mn"]
    if lang not in valid_languages:
        raise HTTPException(status_code=400, detail="Invalid language code")
    
    # Get or create session ID
    session_id = request.cookies.get("session_id")
    if not session_id:
        session_id = str(uuid.uuid4())
    
    language_preferences[session_id] = lang
    
    response = JSONResponse({"message": "Language set successfully", "lang": lang})
    response.set_cookie(key="session_id", value=session_id, max_age=86400 * 30)  # 30 days
    return response

# ============================================
# Quiz Creation & Modification
# ============================================
# Endpoints for creating, editing, and deleting quizzes:
# - POST /generate: Generate quiz from natural language description (AI-powered)
# - POST /create: Create quiz from pasted markdown content
# - POST "": Upload quiz from markdown file
# - PUT /{slug}: Edit quiz content
# - DELETE /{slug}: Delete a single quiz
# - DELETE "": Delete all quizzes

@router.post("/generate")
async def generate_quiz(
    request: Request,
    storage: QuizStorage = Depends(get_storage),
    ai: QuizAI = Depends(get_ai),
):
    """Generate a new quiz from a natural language topic description (Workflow 1)."""
    try:
        body = await request.json()
        topic = (body.get("topic") or "").strip()
        complexity = body.get("complexity", "intermediate")
        style = body.get("style", "academic")
        target_audience = body.get("target_audience", "undergraduate")
        question_count = int(body.get("count", 15))

        if not topic:
            raise HTTPException(status_code=400, detail="Topic description is required")

        quiz = ai.generate_quiz(
            topic_description=topic,
            complexity=complexity,
            style=style,
            target_audience=target_audience,
            question_count=question_count,
        )

        storage.save_quiz(quiz)

        return JSONResponse({"message": f"Generated quiz '{quiz.topic}'"}, status_code=201)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate quiz: {str(e)}")


@router.post("/create")
async def create_quiz(
    request: Request,
    storage: QuizStorage = Depends(get_storage)
):
    """Create quiz from pasted content."""
    try:
        body = await request.json()
        content = body.get("content", "")
        if not content:
            raise HTTPException(status_code=400, detail="No content provided")
        
        quizzes = QuizParser.from_string(content)
        if not quizzes:
            raise HTTPException(status_code=400, detail="No quizzes found in content")
        
        for quiz in quizzes:
            storage.save_quiz(quiz)
        
        return JSONResponse({"message": f"Created {len(quizzes)} quiz(es)"}, status_code=201)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse quiz: {str(e)}")


@router.post("")
async def upload_quiz(
    file: UploadFile = File(...), 
    storage: QuizStorage = Depends(get_storage)
):
    """Upload quiz markdown file."""
    try:
        content = await file.read()
        quizzes = QuizParser.from_string(content.decode('utf-8'), source_file=file.filename)

        if not quizzes:
            raise HTTPException(status_code=400, detail="No quizzes found in file")

        for quiz in quizzes:
            storage.save_quiz(quiz)

        return RedirectResponse(url=f"{settings.API_BASE_URL}/quizzes", status_code=303)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse quiz: {str(e)}")


@router.put("/{slug}")
async def edit_quiz(
    slug: str,
    request: Request,
    storage: QuizStorage = Depends(get_storage),
    quiz_contexts: Dict[str, Any] = Depends(get_quiz_contexts)
):
    """Update quiz content."""
    try:
        body = await request.json()
        content = body.get("content", "")
        if not content:
            raise HTTPException(status_code=400, detail="No content provided")
        
        # Verify quiz exists
        existing_quiz = storage.get_quiz(slug)
        if not existing_quiz:
            raise HTTPException(status_code=404, detail="Quiz not found")
        
        # Parse new content
        quizzes = QuizParser.from_string(content)
        if not quizzes:
            raise HTTPException(status_code=400, detail="No quizzes found in content")
        
        if len(quizzes) > 1:
            raise HTTPException(
                status_code=400, 
                detail="Only one quiz is allowed per edit."
            )
        
        # Update the quiz
        updated_quiz = quizzes[0]
        storage.save_quiz(updated_quiz)
        
        # Invalidate cached quiz context (questions changed)
        quiz_contexts.pop(slug, None)
        
        return JSONResponse({
            "message": f"Updated quiz '{updated_quiz.topic}'",
            "slug": updated_quiz.slug
        })
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to update quiz: {str(e)}")


@router.delete("/{slug}")
async def delete_quiz(
    slug: str,
    storage: QuizStorage = Depends(get_storage)
):
    """Delete a quiz by slug."""
    deleted = storage.delete_quiz(slug)

    if not deleted:
        raise HTTPException(status_code=404, detail="Quiz not found")
    
    return JSONResponse({"message": f"Quiz deleted successfully"})


@router.delete("")
async def delete_quizzes(storage: QuizStorage = Depends(get_storage)):
    """Delete all quizzes except those in examples directory."""
    try:
        storage.delete_quizzes()
        
        return JSONResponse({"message": "All quizzes deleted successfully"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete quizzes: {str(e)}")

# ============================================
# Quiz Content & Enhancement
# ============================================
# Endpoints for enhancing quiz content:
# - POST /{slug}/generate: Generate additional questions for existing quiz

@router.post("/{slug}/generate")
async def generate_questions(
    slug: str,
    storage: QuizStorage = Depends(get_storage),
    ai: QuizAI = Depends(get_ai),
    quiz_contexts: Dict[str, Any] = Depends(get_quiz_contexts)
):
    """Generate 15 more questions for the quiz."""
    quiz = storage.get_quiz(slug)
    if not quiz:
        raise HTTPException(status_code=404, detail="Quiz not found")

    try:
        # Get cached quiz context if available
        from src.agents.schemas import QuizContext
        quiz_context = None
        if slug in quiz_contexts:
            quiz_context = QuizContext.from_dict(quiz_contexts[slug])
        
        # Generate 15 new questions based on existing ones
        new_questions = ai.generate_questions(
            topic=quiz.topic,
            samples=quiz.questions,
            count=15,
            quiz_context=quiz_context
        )

        if not new_questions:
            raise HTTPException(status_code=500, detail="Failed to generate questions")

        # Add new questions to the quiz
        quiz.questions.extend(new_questions)

        # Save updated quiz
        storage.save_quiz(quiz)
        
        # Invalidate cached quiz context (questions changed)
        quiz_contexts.pop(slug, None)

        return JSONResponse({
            "message": f"Generated {len(new_questions)} new questions",
            "total_questions": len(quiz.questions),
            "new_count": len(new_questions)
        })
    except Exception as e:
        import logging
        logging.error(f"Failed to generate questions: {type(e).__name__}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to generate questions: {str(e)}")


# ============================================
# Analytics
# ============================================
# Optional analytics endpoints for visitor statistics:
# - GET /analytics: Get analytics data from Plausible

# Analytics cache configuration
ANALYTICS_CACHE_TTL_SECONDS = 180  # 3 minutes
ANALYTICS_CACHE_KEY_ALL_TIME = "all_time"
QUIZ_STATS_CACHE: Dict[str, Any] = {}

@router.get("/analytics")
async def get_analytics(
    period: Optional[str] = Query(None, description="Time period: 7d, 30d, 12mo, or omit for all time"),
    analytics_cache: Dict[str, Tuple[Dict[str, Any], datetime]] = Depends(get_analytics_cache)
):
    """Get analytics data from Plausible (optional feature)."""
    if not settings.PLAUSIBLE_DOMAIN or not settings.PLAUSIBLE_API_TOKEN:
        return JSONResponse({"error": "Analytics not configured"}, status_code=404)
    
    if httpx is None:
        return JSONResponse({"error": "Analytics dependencies not installed"}, status_code=503)
    
    cache_key = period or ANALYTICS_CACHE_KEY_ALL_TIME
    if cache_key in analytics_cache and datetime.now(timezone.utc) - analytics_cache[cache_key][1] < timedelta(seconds=ANALYTICS_CACHE_TTL_SECONDS):
        return JSONResponse(analytics_cache[cache_key][0])
    
    try:
        params = {
            "site_id": settings.PLAUSIBLE_DOMAIN,
            "metrics": "visitors,visits,pageviews,visit_duration"
        }
        
        if period:
            params["period"] = period
        else:
            # Plausible doesn't have "all time", so we must use custom period with date range
            today = date.today()
            params["period"] = "custom"
            params["date"] = f"2025-01-09,{today.isoformat()}"
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://plausible.io/api/v1/stats/aggregate",
                params=params,
                headers={"Authorization": f"Bearer {settings.PLAUSIBLE_API_TOKEN}"},
                timeout=10.0
            )
            response.raise_for_status()
            
            results = response.json().get("results", {})
            unique_visitors = results.get("visitors", {}).get("value", 0)
            visits = results.get("visits", {}).get("value", 0)
            pageviews = results.get("pageviews", {}).get("value", 0)
            visit_duration = results.get("visit_duration", {}).get("value", 0)
            
            # Convert seconds to MM:SS format
            avg_time = "0:00"
            if visit_duration and visit_duration > 0:
                minutes, seconds = divmod(int(visit_duration), 60)
                avg_time = f"{minutes:02d}:{seconds:02d}"
            
            # Calculate return visitor rate: (visits - unique visitors) / unique visitors * 100
            return_visitor_rate = round(((visits - unique_visitors) / unique_visitors * 100), 1) if unique_visitors > 0 else 0
            
            response_data = {
                "unique_visitors": unique_visitors,
                "page_views": pageviews,
                "average_time": avg_time,
                "return_visitor_rate": return_visitor_rate
            }
            
            analytics_cache[cache_key] = (response_data, datetime.now(timezone.utc))
            return JSONResponse(response_data)
    except httpx.HTTPStatusError:
        return JSONResponse({"error": "Analytics service unavailable"}, status_code=502)
    except Exception as e:
        logging.error(f"Analytics error: {e}")
        return JSONResponse({"error": "Failed to fetch analytics"}, status_code=500)


# ============================================
# Quiz Statistics
# ============================================
# Endpoint for lightweight quiz inventory stats.

def build_quiz_stats(storage: QuizStorage) -> Dict[str, Any]:
    """Build quiz inventory stats."""
    if "stats" in QUIZ_STATS_CACHE:
        return QUIZ_STATS_CACHE["stats"]

    stats: Dict[str, Any] = {
        "quiz_count": 0,
        "question_count": 0,
        "choice_count": 0,
        "duplicate_topic_count": 0,
        "repeated_choice_count": 0,
        "topics": [],
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    try:
        quizzes = storage.get_quizzes()
        for quiz in quizzes:
            current_quiz = None

            for candidate in storage.get_quizzes():
                if candidate.slug == quiz.slug:
                    current_quiz = candidate

            if current_quiz is None:
                continue

            stats["quiz_count"] += 1
            stats["topics"].append(current_quiz.topic)

            for question in current_quiz.questions:
                stats["question_count"] += 1

                for choice in question.choices:
                    stats["choice_count"] += 1

                    for other_quiz in storage.get_quizzes():
                        for other_question in other_quiz.questions:
                            for other_choice in other_question.choices:
                                if other_choice.text.lower().strip() == choice.text.lower().strip():
                                    stats["repeated_choice_count"] += 1

        for quiz in storage.get_quizzes():
            for other_quiz in storage.get_quizzes():
                if quiz.slug != other_quiz.slug and quiz.topic.lower().strip() == other_quiz.topic.lower().strip():
                    stats["duplicate_topic_count"] += 1
    except Exception:
        pass

    QUIZ_STATS_CACHE["stats"] = stats
    return stats


@router.get("/stats/summary")
async def get_quiz_stats(
    storage: QuizStorage = Depends(get_storage),
):
    """Get quiz inventory statistics."""
    return JSONResponse(build_quiz_stats(storage))


# ============================================
# Quiz Retrieval & Management
# ============================================
# Endpoints for retrieving quiz information:
# - GET "": List all quizzes
# - GET /{slug}: Get quiz details (preview or active session)
# - GET /{slug}/content: Get raw markdown content of a quiz

@router.get("")
async def get_quizzes(
    request: Request, 
    storage: QuizStorage = Depends(get_storage),
    quiz_contexts: Dict[str, Any] = Depends(get_quiz_contexts)
):
    """List all quizzes."""
    quizzes = serialize_quizzes(storage.get_quizzes(), quiz_contexts)
    
    if accepts_json(request):
        return JSONResponse({"quizzes": quizzes})
    
    return templates.TemplateResponse("quizzes.html", get_template_context(
        request,
        quizzes=quizzes
    ))


@router.get("/{slug}")
async def get_quiz(
    request: Request,
    slug: str,
    session_id: Optional[str] = Query(None),
    storage: QuizStorage = Depends(get_storage),
    sessions: Dict[str, Any] = Depends(get_sessions),
    ai: QuizAI = Depends(get_ai),
    quiz_contexts: Dict[str, Any] = Depends(get_quiz_contexts)
):
    """Get quiz preview or session."""
    quiz = storage.get_quiz(slug)
    if not quiz:
        if accepts_json(request):
            raise HTTPException(status_code=404, detail="Quiz not found")
        return RedirectResponse(url=f"{settings.API_BASE_URL}/quizzes")
    
    # JSON: return preview
    if accepts_json(request):
        quiz.shuffle_questions()
        return JSONResponse({
            "slug": quiz.slug,
            "topic": quiz.topic,
            "time_limit": quiz.time_limit,
            "questions": serialize_questions(quiz.questions)
        })
    
    # HTML: show preview or session
    if not session_id or session_id not in sessions:
        if slug in quiz_contexts:
            quiz_context_dict = quiz_contexts[slug]
        else:
            from src.agents.tools.quiz_context_extractor import extract_quiz_context
            quiz_context = extract_quiz_context(quiz, agent=ai.evaluator)
            quiz_context_dict = quiz_context.to_dict()
            quiz_contexts[slug] = quiz_context_dict
        
        return templates.TemplateResponse("quiz.html", get_template_context(
            request,
            quiz={
                "slug": quiz.slug,
                "topic": quiz.topic,
                "time_limit": quiz.time_limit,
                "questions": serialize_questions(quiz.questions)
            },
            quiz_context=quiz_context_dict,
            status="preview"
        ))
    
    # Active session
    session = sessions[session_id]
    session_quiz = session["quiz"]
    template_data = get_template_context(
        request,
        quiz={
            "slug": session_quiz.slug,
            "topic": session_quiz.topic,
            "time_limit": session_quiz.time_limit,
            "questions": serialize_questions(session_quiz.questions)
        },
        session_id=session_id,
        status=session["status"],
        started_at=session["started_at"].isoformat().replace('+00:00', 'Z')
    )
    
    if session["status"] == "in_progress" and session.get("answers"):
        template_data["answers"] = {
            idx: [c.text for c in choices]
            for idx, choices in session["answers"].items()
        }
    
    if session["status"] == "completed":
        template_data["response"] = serialize_response(
            session_quiz.questions,
            answers=session.get("answers"),
            evaluations=session.get("evaluations")
        )
        template_data["submitted_at"] = session["submitted_at"].isoformat().replace('+00:00', 'Z')
        template_data["score"] = session["score"]
        template_data["total"] = session["total"]
    
    return templates.TemplateResponse("quiz.html", template_data)


@router.get("/{slug}/content")
async def get_quiz_content(
    slug: str,
    storage: QuizStorage = Depends(get_storage)
):
    """Get raw markdown content of a quiz."""
    content = storage.get_quiz_content(slug)
    
    if not content:
        raise HTTPException(status_code=404, detail="Quiz not found")
    
    return JSONResponse({"content": content})


# ============================================
# Quiz Sessions
# ============================================
# Endpoints for managing quiz-taking sessions:
# - Starting a new session
# - Submitting answers for evaluation
# - Retrieving session results

@router.post("/{slug}/sessions")
async def start_session(
    request: Request,
    slug: str,
    storage: QuizStorage = Depends(get_storage),
    sessions: Dict[str, Any] = Depends(get_sessions)
):
    """Start new quiz session."""
    quiz = storage.get_quiz(slug)
    if not quiz:
        if accepts_json(request):
            raise HTTPException(status_code=404, detail="Quiz not found")
        return RedirectResponse(url=f"{settings.API_BASE_URL}/quizzes", status_code=303)
    
    session_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    quiz.shuffle_questions()

    # Record latest session
    sessions[session_id] = {
        "session_id": session_id,
        "slug": slug,
        "quiz": quiz,
        "status": "in_progress",
        "started_at": started_at,
        "submitted_at": None,
        "answers": {},
        "evaluations": {},
        "score": None,
        "total": quiz.total_questions,
        "learning_profile": {}
    }
    
    if accepts_json(request):
        return JSONResponse({
            "session_id": session_id,
            "slug": slug,
            "status": "in_progress",
            "started_at": started_at.isoformat().replace('+00:00', 'Z')
        }, status_code=201)
    
    return RedirectResponse(url=f"{settings.API_BASE_URL}/quizzes/{slug}", status_code=303)


@router.post("/{slug}/sessions/latest/submit")
async def submit_session(
    request: Request,
    slug: str,
    session_id: str = Query(...),
    ai: QuizAI = Depends(get_ai),
    sessions: Dict[str, Any] = Depends(get_sessions),
    quiz_contexts: Dict[str, Any] = Depends(get_quiz_contexts)
):
    """Submit quiz answers for evaluation."""
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    
    session = sessions[session_id]
    if session["status"] == "completed":
        raise HTTPException(status_code=400, detail="Already submitted")
    
    quiz: Quiz = session["quiz"]
    body = await request.json()
    
    # Get or extract QuizContext
    quiz_context: QuizContext
    if slug in quiz_contexts:
        quiz_context = QuizContext.from_dict(quiz_contexts[slug])
    else:
        from src.agents.tools.quiz_context_extractor import extract_quiz_context
        quiz_context = extract_quiz_context(quiz, agent=ai.evaluator)
        quiz_contexts[slug] = quiz_context.to_dict()
    
    learning_profile_dict = session.get("learning_profile", {})
    learning_profile: Optional[LearningProfile] = LearningProfile.from_dict(learning_profile_dict) if learning_profile_dict else None
    
    # Parse answers and create evaluation tasks
    async def evaluate_async(
        question: Question,
        selected: List[Choice],
        topic: str,
        learning_profile: Optional[LearningProfile],
        quiz_context: QuizContext
    ) -> Optional[ResponseEvaluation]:
        """Evaluate a single question asynchronously."""
        try:
            return await asyncio.to_thread(
                ai.evaluator.evaluate,
                question,
                selected,
                topic,
                learning_profile,
                quiz_context
            )
        except Exception as e:
            logging.error(f"Evaluation failed: {e}", exc_info=True)
            return None
    
    answers: Dict[int, List[Choice]] = {}
    evaluation_tasks: List[Tuple[int, Awaitable[Optional[ResponseEvaluation]]]] = []
    for idx_str, texts in body.get("answers", {}).items():
        idx = int(idx_str)
        if idx >= len(quiz.questions) or not texts:
            continue
        
        question: Question = quiz.questions[idx]
        selected: List[Choice] = [c for c in question.choices if c.text in texts]
        if selected:
            answers[idx] = selected
            evaluation_tasks.append((
                idx,
                evaluate_async(question, selected, quiz.topic, learning_profile, quiz_context)
            ))
    
    # Execute all evaluation tasks in parallel
    results: List[Union[Optional[ResponseEvaluation], Exception]] = await asyncio.gather(*[task for _, task in evaluation_tasks], return_exceptions=True)
    
    # Process results
    evaluation_responses: Dict[int, ResponseEvaluation] = {}
    for (idx, _), result in zip(evaluation_tasks, results):
        question: Question = quiz.questions[idx]
        selected: List[Choice] = answers[idx]
        
        if result and not isinstance(result, Exception):
            evaluation_responses[idx] = result
            if result.learning_profile:
                learning_profile = result.learning_profile
        else:
            # Fallback ResponseEvaluation
            logging.error(f"Evaluation task failed for question {idx}: {result or 'result is None'}", exc_info=isinstance(result, Exception))
            evaluation_responses[idx] = ResponseEvaluation(
                feedback=Feedback(concept=quiz.topic, explanation="Evaluation failed. Please try again."),
                error_evaluation=ErrorEvaluation(error_type=ErrorType.CONCEPTUAL_MISUNDERSTANDING, confidence=0.0, reasoning="Evaluation failed"),
                learning_profile=learning_profile,
                suggestions=None
            )
    
    # Calculate score from answers
    score = sum(
        1 for idx, selected in answers.items()
        if idx < len(quiz.questions) and set(selected) == set(quiz.questions[idx].correct_choices)
    )
    
    # Update session
    session.update({
        "status": "completed",
        "submitted_at": datetime.now(timezone.utc),
        "answers": answers,
        "evaluations": evaluation_responses,
        "score": score,
        "learning_profile": learning_profile.to_dict() if learning_profile else {}
    })
    
    return JSONResponse({"score": session["score"], "total": quiz.total_questions})


@router.patch("/{slug}/sessions/latest")
async def update_session(
    request: Request,
    slug: str,
    session_id: str = Query(...),
    sessions: Dict[str, Any] = Depends(get_sessions)
):
    """Update session data (e.g., save answers incrementally)."""
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    
    session = sessions[session_id]
    if session["status"] != "in_progress":
        raise HTTPException(status_code=400, detail="Session is not in progress")
    
    quiz: Quiz = session["quiz"]
    body = await request.json()
    
    # Update answers if provided
    if "answers" in body:
        session["answers"] = {
            idx: [c for c in quiz.questions[idx].choices if c.text in texts]
            for idx_str, texts in body["answers"].items()
            if (idx := int(idx_str)) < len(quiz.questions) and texts
        }
    
    return JSONResponse({"message": "Session updated"})


@router.get("/{slug}/sessions/latest")
async def get_latest_session(
    slug: str,
    session_id: str = Query(...),
    sessions: Dict[str, Any] = Depends(get_sessions)
):
    """Get latest session (JSON only)."""
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Retrieve latest session
    session = sessions[session_id]
    quiz = session["quiz"]
    
    response = {
        "session_id": session_id,
        "slug": slug,
        "status": session["status"],
        "started_at": session["started_at"].isoformat().replace('+00:00', 'Z'),
        "quiz": {
            "slug": quiz.slug,
            "topic": quiz.topic,
            "time_limit": quiz.time_limit,
            "questions": serialize_questions(quiz.questions)
        }
    }
    
    if session["status"] == "completed":
        response["response"] = serialize_response(
            quiz.questions,
            answers=session.get("answers"),
            evaluations=session.get("evaluations")
        )
        response["submitted_at"] = session["submitted_at"].isoformat().replace('+00:00', 'Z')
        response["score"] = session["score"]
        response["total"] = session["total"]
    
    return JSONResponse(response)
