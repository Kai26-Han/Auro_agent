"""Deterministic orchestration evaluation over public results and traces."""

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


EVALUATOR_VERSION = 1
DIMENSIONS = ("completion","planning","tools","evidence","review","artifact","efficiency")


def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Expectations(StrictModel):
    allowed_statuses: list[str] = Field(default_factory=lambda:["completed"])
    required_stages: list[str] = Field(default_factory=list)
    min_stage_count: int = Field(default=0, ge=0, le=30)
    required_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    min_tool_calls: int = Field(default=0, ge=0, le=100)
    require_citations: bool = False
    min_citations: int = Field(default=0, ge=0, le=100)
    require_review: bool = False
    min_rework: int = Field(default=0, ge=0, le=3)
    require_artifact: bool = False
    require_approval: bool = False
    max_model_calls: int | None = Field(default=None, ge=1, le=1000)
    max_tokens: int | None = Field(default=None, ge=1, le=10_000_000)
    max_duration_ms: int | None = Field(default=None, ge=1, le=86_400_000)


class EvalCase(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    task: str = Field(min_length=1, max_length=10000)
    capability_kind: Literal["assistant","workflow","team","any"] = "any"
    tags: list[str] = Field(default_factory=list, max_length=20)
    expectations: Expectations = Field(default_factory=Expectations)
    weights: dict[str,float]
    pass_score: float = Field(default=80, ge=0, le=100)
    hard_requirements: list[Literal["status","artifact","approval","citations","forbidden_tools"]] = Field(default_factory=lambda:["status"])

    @model_validator(mode="after")
    def valid_weights(self):
        if set(self.weights) != set(DIMENSIONS): raise ValueError("评测维度权重不完整。")
        if any(value < 0 for value in self.weights.values()) or abs(sum(self.weights.values())-100)>0.001:
            raise ValueError("评测维度权重必须为非负数且合计 100。")
        return self


class EvalSuite(StrictModel):
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    version: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    cases: list[EvalCase] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_cases(self):
        if len({case.id for case in self.cases}) != len(self.cases): raise ValueError("评测任务 ID 重复。")
        return self


class EvaluateRequest(StrictModel):
    job_id: str = Field(min_length=1, max_length=128)
    suite_id: str = Field(default="agent-orchestration", min_length=1, max_length=64)
    case_id: str = Field(min_length=1, max_length=64)
    notes: str = Field(default="", max_length=2000)


class CompareRequest(StrictModel):
    baseline_report_id: str = Field(min_length=1, max_length=128)
    candidate_report_id: str = Field(min_length=1, max_length=128)


def _duration_ms(trace):
    root=next((s for s in trace["spans"] if s["span_id"]==trace["root_span_id"]),None)
    if not root or not root.get("started_at") or not root.get("ended_at"): return None
    try:
        start=datetime.fromisoformat(root["started_at"]);end=datetime.fromisoformat(root["ended_at"])
        return max(0,int((end-start).total_seconds()*1000))
    except ValueError:return None


def _citations(output):
    double=[f"[[{value}]]" for value in re.findall(r"\[\[([^\]\n]+)\]\]",output)]
    lines=re.findall(r"\[[^\[\]\n]+:L\d+(?:-L\d+)?\]",output)
    return list(dict.fromkeys(double+lines))


def _source_citations(sources):
    values=set()
    for source in sources or []:
        if source.get("citation"): values.add(str(source["citation"]))
        if source.get("chunk_id"): values.add("[["+str(source["chunk_id"])+"]]")
        if source.get("path") and source.get("start"):
            end=source.get("end",source["start"])
            values.add(f"[{source['path']}:L{source['start']}-L{end}]")
            if end==source["start"]:values.add(f"[{source['path']}:L{source['start']}]")
    return values


def _ratio(found, required):
    return 100.0 if not required else 100.0*len(set(found)&set(required))/len(set(required))


def _budget(value, maximum):
    if maximum is None:return None
    if value is None:return 0.0
    return 100.0 if value<=maximum else max(0.0,100.0*maximum/value)


def score_run(case, job, trace):
    case=EvalCase.model_validate(case)
    result=job.get("result") or {}
    status=job.get("status")
    spans=trace["spans"]
    tools=[s["attributes"].get("tool_id") or s["name"] for s in spans if s["span_type"] in {"tool","retrieval"}]
    failed_tools=[s for s in spans if s["span_type"] in {"tool","retrieval"} and s["status"]!="completed"]
    stage_spans=[s for s in spans if s["span_type"]=="stage"]
    latest={}
    for span in stage_spans:
        node=span["attributes"].get("node_id") or span["name"]
        if node not in latest or span["attributes"].get("attempt",0)>=latest[node]["attributes"].get("attempt",0):latest[node]=span
    stage_ids=set(latest)
    expected=case.expectations
    completion=100.0 if status in expected.allowed_statuses else 0.0
    stage_coverage=_ratio(stage_ids,expected.required_stages)
    count_score=100.0 if len(stage_ids)>=expected.min_stage_count else 100.0*len(stage_ids)/max(1,expected.min_stage_count)
    final_stage_score=100.0 if not latest else 100.0*sum(s["status"] in {"completed","skipped"} for s in latest.values())/len(latest)
    planning=(stage_coverage+count_score+final_stage_score)/3
    required_tools=_ratio(tools,expected.required_tools)
    tool_count=100.0 if len(tools)>=expected.min_tool_calls else 100.0*len(tools)/max(1,expected.min_tool_calls)
    reliability=100.0 if not tools else 100.0*(len(tools)-len(failed_tools))/len(tools)
    forbidden=sorted(set(tools)&set(expected.forbidden_tools))
    approvals=[s for s in spans if s["span_type"]=="approval" and s["status"]=="completed"]
    approval_score=100.0 if not expected.require_approval or approvals else 0.0
    tool_score=0.0 if forbidden else (required_tools+tool_count+reliability+approval_score)/4
    citations=_citations(str(result.get("output") or ""));known=_source_citations(result.get("sources",[]))
    citation_count=100.0 if not expected.require_citations or len(citations)>=max(1,expected.min_citations) else 100.0*len(citations)/max(1,expected.min_citations)
    citation_valid=100.0 if not expected.require_citations else (100.0*sum(c in known for c in citations)/len(citations) if citations else 0.0)
    evidence=(citation_count+citation_valid)/2
    review_spans=[s for s in latest.values() if s["attributes"].get("kind")=="review"]
    review_ok=not expected.require_review or bool(review_spans and all(s["status"]=="completed" and s["attributes"].get("approved") is True for s in review_spans))
    attempts={}
    for span in stage_spans:
        node=span["attributes"].get("node_id") or span["name"]
        attempts[node]=max(attempts.get(node,0),int(span["attributes"].get("attempt") or 0))
    reworks=max((value-1 for value in attempts.values()),default=0)
    rework_ok=reworks>=expected.min_rework
    review=50.0*review_ok+50.0*rework_ok
    artifacts=[s for s in spans if s["span_type"]=="artifact" and s["status"]=="completed"]
    verified=[s for s in artifacts if s["attributes"].get("sha256") and s["attributes"].get("bytes") is not None]
    artifact=100.0 if not expected.require_artifact else (100.0 if verified else 0.0)
    usage=trace.get("usage",{});duration=_duration_ms(trace)
    budgets=[v for v in (_budget(usage.get("model_calls"),expected.max_model_calls),
                          _budget(usage.get("usage_tokens"),expected.max_tokens),
                          _budget(duration,expected.max_duration_ms)) if v is not None]
    efficiency=sum(budgets)/len(budgets) if budgets else 100.0
    dimensions={"completion":completion,"planning":planning,"tools":tool_score,"evidence":evidence,
                "review":review,"artifact":artifact,"efficiency":efficiency}
    dimensions={key:round(value,2) for key,value in dimensions.items()}
    score=round(sum(dimensions[key]*case.weights[key]/100 for key in DIMENSIONS),2)
    hard=[]
    checks={"status":completion==100,"artifact":bool(verified) or not expected.require_artifact,
            "approval":bool(approvals) or not expected.require_approval,
            "citations":citation_count==100 and citation_valid==100,
            "forbidden_tools":not forbidden}
    for requirement in case.hard_requirements:
        if not checks[requirement]:hard.append(requirement)
    return {"evaluator_version":EVALUATOR_VERSION,"case_id":case.id,"case_name":case.name,
            "score":score,"passed":score>=case.pass_score and not hard,"pass_score":case.pass_score,
            "dimensions":dimensions,"hard_failures":hard,
            "metrics":{"status":status,"stage_count":len(stage_ids),"stage_attempts":sum(attempts.values()),
                       "reworks":reworks,"tool_calls":len(tools),"failed_tool_calls":len(failed_tools),
                       "citations":len(citations),"valid_citations":sum(c in known for c in citations),
                       "artifacts":len(artifacts),"verified_artifacts":len(verified),"approvals":len(approvals),
                       "model_calls":usage.get("model_calls",0),"usage_tokens":usage.get("usage_tokens",0),
                       "usage_unknown":usage.get("usage_unknown",False),"duration_ms":duration,
                       "estimated_cost":None,"cost_basis":"token usage; currency price is not configured"},
            "observed":{"stages":sorted(stage_ids),"tools":tools,"forbidden_tools":forbidden,
                        "citation_tokens":citations}}


class EvaluationService:
    def __init__(self, settings, jobs):
        self.jobs=jobs;self.path=settings.data_dir/"evaluations.sqlite"
        suite_dir=Path(__file__).with_name("evaluation_suites")
        self.suites={}
        for path in sorted(suite_dir.glob("*.json")):
            suite=EvalSuite.model_validate_json(path.read_text())
            self.suites[suite.id]=suite
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS reports(
                id TEXT PRIMARY KEY,suite_id TEXT,suite_version TEXT,case_id TEXT,job_id TEXT,trace_id TEXT,
                capability_id TEXT,capability_version TEXT,score REAL,passed INTEGER,data TEXT,created TEXT)''')

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=20);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    def suite_list(self):
        return [{"id":s.id,"version":s.version,"name":s.name,"description":s.description,
                 "cases":[{"id":c.id,"name":c.name,"description":c.description,"task":c.task,
                           "capability_kind":c.capability_kind,"tags":c.tags,"pass_score":c.pass_score}
                          for c in s.cases]} for s in self.suites.values()]

    def evaluate(self, body):
        body=EvaluateRequest.model_validate(body);suite=self.suites.get(body.suite_id)
        if suite is None:raise ValueError("评测集不存在。")
        case=next((c for c in suite.cases if c.id==body.case_id),None)
        if case is None:raise ValueError("评测任务不存在。")
        job=self.jobs.get(body.job_id)
        actual_kind="team" if job["capability_id"].startswith("team-") else "workflow" if job["capability_id"].startswith("workflow-") else "assistant"
        if case.capability_kind not in {"any",actual_kind}:raise ValueError("任务使用的能力类型与评测任务不匹配。")
        if job["status"] in {"queued","running","stopping","waiting_approval"}:raise ValueError("任务尚未结束，不能评分。")
        trace=self.jobs.trace(body.job_id);scored=score_run(case,job,trace)
        identity=json.dumps([suite.id,suite.version,case.id,job["id"],trace["trace_id"],EVALUATOR_VERSION],separators=(",",":"))
        report_id="eval-"+hashlib.sha256(identity.encode()).hexdigest()[:24]
        data={**scored,"report_id":report_id,"suite_id":suite.id,"suite_version":suite.version,"job_id":job["id"],
              "trace_id":trace["trace_id"],"capability_id":job["capability_id"],"capability_version":job["capability_version"],
              "notes":body.notes,"created":now()}
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO reports VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (report_id,suite.id,suite.version,case.id,job["id"],trace["trace_id"],job["capability_id"],
                        job["capability_version"],data["score"],int(data["passed"]),json.dumps(data,ensure_ascii=False),data["created"]))
        return data

    def list(self,limit=50):
        with self.db() as db:rows=db.execute("SELECT data FROM reports ORDER BY created DESC,rowid DESC LIMIT ?",(limit,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def get(self,report_id):
        with self.db() as db:row=db.execute("SELECT data FROM reports WHERE id=?",(report_id,)).fetchone()
        if row is None:raise ValueError("评测报告不存在。")
        return json.loads(row[0])

    def compare(self,body):
        body=CompareRequest.model_validate(body);before=self.get(body.baseline_report_id);after=self.get(body.candidate_report_id)
        if (before["suite_id"],before["suite_version"],before["case_id"])!=(after["suite_id"],after["suite_version"],after["case_id"]):
            raise ValueError("只能比较同一版本评测集中的同一任务。")
        dimension_delta={key:round(after["dimensions"][key]-before["dimensions"][key],2) for key in DIMENSIONS}
        metric_delta={}
        for key in ("model_calls","usage_tokens","duration_ms","tool_calls","failed_tool_calls","valid_citations","verified_artifacts","reworks"):
            a,b=after["metrics"].get(key),before["metrics"].get(key)
            metric_delta[key]=None if a is None or b is None else a-b
        return {"suite_id":before["suite_id"],"suite_version":before["suite_version"],"case_id":before["case_id"],
                "baseline_report_id":before["report_id"],"candidate_report_id":after["report_id"],
                "score_delta":round(after["score"]-before["score"],2),"pass_changed":before["passed"]!=after["passed"],
                "dimension_delta":dimension_delta,"metric_delta":metric_delta}
