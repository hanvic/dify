import contextvars
import json
import logging
import time
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import click
from celery import shared_task  # type: ignore
from flask import current_app, g
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from configs import dify_config
from core.app.entities.app_invoke_entities import InvokeFrom, RagPipelineGenerateEntity
from core.app.entities.rag_pipeline_invoke_entities import RagPipelineInvokeEntity
from core.rag.pipeline.queue import TenantIsolatedTaskQueue
from core.repositories.factory import DifyCoreRepositoryFactory
from extensions.ext_database import db
from models import Account, Tenant
from models.dataset import Pipeline
from models.enums import WorkflowRunTriggeredFrom
from models.workflow import Workflow, WorkflowNodeExecutionTriggeredFrom
from services.file_service import FileService

logger = logging.getLogger(__name__)


@shared_task(queue="priority_pipeline")
def priority_rag_pipeline_run_task(
    rag_pipeline_invoke_entities_file_id: str,
    tenant_id: str,
):
    """
    Async Run rag pipeline task using high priority queue.

    :param rag_pipeline_invoke_entities_file_id: File ID containing serialized RAG pipeline invoke entities
    :param tenant_id: Tenant ID for the pipeline execution
    """
    # run with threading, thread pool size is 10

    try:
        start_at = time.perf_counter()
        rag_pipeline_invoke_entities_content = FileService(db.engine).get_file_content(
            rag_pipeline_invoke_entities_file_id
        )
        rag_pipeline_invoke_entities = json.loads(rag_pipeline_invoke_entities_content)

        logger.info("tenant %s received %d rag pipeline invoke entities", tenant_id, len(rag_pipeline_invoke_entities))

        # Get Flask app object for thread context
        flask_app = current_app._get_current_object()  # type: ignore

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = []
            for rag_pipeline_invoke_entity in rag_pipeline_invoke_entities:
                # Submit task to thread pool with Flask app
                future = executor.submit(run_single_rag_pipeline_task, rag_pipeline_invoke_entity, flask_app)
                futures.append(future)

            # Wait for all tasks to complete
            for future in futures:
                try:
                    future.result()  # This will raise any exceptions that occurred in the thread
                except Exception:
                    logging.exception("Error in pipeline task")
        end_at = time.perf_counter()
        logging.info(
            click.style(
                f"tenant_id: {tenant_id}, Rag pipeline run completed. Latency: {end_at - start_at}s", fg="green"
            )
        )
    except Exception:
        logging.exception(click.style(f"Error running rag pipeline, tenant_id: {tenant_id}", fg="red"))
        raise
    finally:
        tenant_isolated_task_queue = TenantIsolatedTaskQueue(tenant_id, "pipeline")

        # Check if there are waiting tasks in the queue
        # Use rpop to get the next task from the queue (FIFO order)
        next_file_ids = tenant_isolated_task_queue.pull_tasks(count=dify_config.TENANT_ISOLATED_TASK_CONCURRENCY)
        logger.info("priority rag pipeline tenant isolation queue %s next files: %s", tenant_id, next_file_ids)

        if next_file_ids:
            for next_file_id in next_file_ids:
                # Process the next waiting task
                # Keep the flag set to indicate a task is running
                tenant_isolated_task_queue.set_task_waiting_time()
                priority_rag_pipeline_run_task.delay(  # type: ignore
                    rag_pipeline_invoke_entities_file_id=next_file_id.decode("utf-8")
                    if isinstance(next_file_id, bytes)
                    else next_file_id,
                    tenant_id=tenant_id,
                )
        else:
            # No more waiting tasks, clear the flag
            tenant_isolated_task_queue.delete_task_key()
        file_service = FileService(db.engine)
        file_service.delete_file(rag_pipeline_invoke_entities_file_id)
        db.session.close()


def run_single_rag_pipeline_task(rag_pipeline_invoke_entity: Mapping[str, Any], flask_app):
    """Run a single RAG pipeline task within Flask app context."""
    # Create Flask application context for this thread
    with flask_app.app_context():
        try:
            rag_pipeline_invoke_entity_model = RagPipelineInvokeEntity.model_validate(rag_pipeline_invoke_entity)
            user_id = rag_pipeline_invoke_entity_model.user_id
            tenant_id = rag_pipeline_invoke_entity_model.tenant_id
            pipeline_id = rag_pipeline_invoke_entity_model.pipeline_id
            workflow_id = rag_pipeline_invoke_entity_model.workflow_id
            streaming = rag_pipeline_invoke_entity_model.streaming
            workflow_execution_id = rag_pipeline_invoke_entity_model.workflow_execution_id
            workflow_thread_pool_id = rag_pipeline_invoke_entity_model.workflow_thread_pool_id
            application_generate_entity = rag_pipeline_invoke_entity_model.application_generate_entity

            with Session(db.engine, expire_on_commit=False) as session:
                # Load required entities
                account = session.scalar(select(Account).where(Account.id == user_id).limit(1))
                if not account:
                    raise ValueError(f"Account {user_id} not found")

                tenant = session.scalar(select(Tenant).where(Tenant.id == tenant_id).limit(1))
                if not tenant:
                    raise ValueError(f"Tenant {tenant_id} not found")
                account.set_current_tenant_with_session(tenant, session=session)

                pipeline = session.scalar(select(Pipeline).where(Pipeline.id == pipeline_id).limit(1))
                if not pipeline:
                    raise ValueError(f"Pipeline {pipeline_id} not found")

                workflow = session.scalar(select(Workflow).where(Workflow.id == pipeline.workflow_id).limit(1))
                if not workflow:
                    raise ValueError(f"Workflow {pipeline.workflow_id} not found")

                # WORKAROUND: 워크플로우 DSL bug 우회.
                # vlm_ocr Tool 노드의 tool_parameters 가 UI 빌더에 의해 비어 저장되는 경우가 있어,
                # datasource 'file' 노드가 출력을 image_file 파라미터로 전달하도록 보정한다.
                # 또한 general_chunker 의 delimiter 가 slash-escape 되는 케이스도 정규화한다.
                try:
                    graph_dict = workflow.graph_dict or {}
                    nodes = graph_dict.get("nodes") or []
                    datasource_id: str | None = None
                    for node in nodes:
                        if (node.get("data") or {}).get("type") == "datasource":
                            datasource_id = node.get("id")
                            break
                    for node in nodes:
                        data = node.get("data") or {}
                        if data.get("type") == "tool" and data.get("tool_name") == "vlm_ocr":
                            tool_params = data.get("tool_parameters") or {}
                            needs_patch = False
                            if "image_file" not in tool_params:
                                needs_patch = True
                            else:
                                # 이미 있지만 잘못된 형태로 저장된 케이스도 보정
                                image_file = tool_params["image_file"] or {}
                                # Pydantic v2 ToolInput 가 MIXED 는 string, VARIABLE 은 list[str] 만 허용.
                                # 잘못된 형태 (mixed value 가 dict 이거나 type 이 다른 enum)이면 교체.
                                value = image_file.get("value")
                                if image_file.get("type") == "variable":
                                    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                                        needs_patch = True
                                    else:
                                        needs_patch = False
                                else:
                                    # mixed 이거나 type 누락/잘못된 enum 이면 VARIABLE 으로 강제.
                                    needs_patch = True
                            if needs_patch and datasource_id is not None:
                                # VARIABLE 타입 value 는 list[str] (selector). datasource 의 file 출력 매핑.
                                tool_params["image_file"] = {
                                    "type": "variable",
                                    "value": [datasource_id, "file"],
                                }
                                data["tool_parameters"] = tool_params
                                logger.warning(
                                    "Patched VLM OCR node %s with image_file -> [%s, file]",
                                    node.get("id"),
                                    datasource_id,
                                )
                        if data.get("type") == "tool" and data.get("tool_name") == "general_chunker":
                            tool_params = data.get("tool_parameters") or {}
                            delim = (tool_params.get("delimiter") or {}).get("value")
                            if delim == "/n,/n/n":
                                tool_params["delimiter"] = {"type": "mixed", "value": "\n,\n\n"}
                                data["tool_parameters"] = tool_params
                                logger.warning("Patched general_chunker delimiter to '\\n,\\n\\n'")
                    # Mutating graph_dict in-place does not persist because it's stored as JSON.
                    # workflow.graph_dict 는 hybrid_property 라 in-place 변경이 반영되지 않을 수 있다.
                    # 명시적으로 graph 컬럼을 update 한다.
                    workflow.graph = json.dumps(graph_dict, ensure_ascii=False)
                    session.add(workflow)
                    session.commit()
                    logger.warning("Persisted patched workflow graph for workflow %s", workflow.id)
                except Exception:
                    logger.exception("Failed to patch workflow graph")

                # DEBUG: dump graph nodes summary so we can inspect variable mappings.
                try:
                    graph_summary = {
                        "pipeline_id": pipeline.id,
                        "workflow_id": workflow.id,
                        "version": workflow.version,
                        "nodes": [
                            {
                                "id": node.get("id"),
                                "type": node.get("data", {}).get("type"),
                                "title": node.get("data", {}).get("title"),
                                "chunk_structure": node.get("data", {}).get("chunk_structure"),
                                "indexing_technique": node.get("data", {}).get("indexing_technique"),
                                "index_chunk_variable_selector": node.get("data", {}).get(
                                    "index_chunk_variable_selector"
                                ),
                                "tool_node_data": {
                                    "tool_name": node.get("data", {}).get("tool_name"),
                                    "provider_name": node.get("data", {}).get("provider_name"),
                                    "tool_label": node.get("data", {}).get("tool_label"),
                                    "tool_parameters": node.get("data", {}).get("tool_parameters"),
                                    "tool_input_data": node.get("data", {}).get("tool_input_data"),
                                    "param_values": node.get("data", {}).get("param_values"),
                                    "variables": node.get("data", {}).get("variables"),
                                    "plugin_id": node.get("data", {}).get("plugin_id"),
                                },
                                "outputs": node.get("data", {}).get("outputs"),
                            }
                            for node in (workflow.graph_dict or {}).get("nodes", [])
                        ],
                        "edges": [
                            {"source": e.get("source"), "target": e.get("target")}
                            for e in (workflow.graph_dict or {}).get("edges", [])
                        ],
                    }
                    logger.warning(
                        "DEBUG_RAG_PIPELINE_GRAPH %s",
                        json.dumps(graph_summary, ensure_ascii=False, indent=2),
                    )
                except Exception:
                    logger.exception("Failed to dump pipeline graph summary")

                if workflow_execution_id is None:
                    workflow_execution_id = str(uuid.uuid4())

                # Create application generate entity from dict
                entity = RagPipelineGenerateEntity.model_validate(application_generate_entity)

                # Create workflow repositories
                session_factory = sessionmaker(bind=db.engine, expire_on_commit=False)
                workflow_execution_repository = DifyCoreRepositoryFactory.create_workflow_execution_repository(
                    session_factory=session_factory,
                    tenant_id=pipeline.tenant_id,
                    user=account,
                    app_id=entity.app_config.app_id,
                    triggered_from=WorkflowRunTriggeredFrom.RAG_PIPELINE_RUN,
                )

                workflow_node_execution_repository = (
                    DifyCoreRepositoryFactory.create_workflow_node_execution_repository(
                        session_factory=session_factory,
                        tenant_id=pipeline.tenant_id,
                        user=account,
                        app_id=entity.app_config.app_id,
                        triggered_from=WorkflowNodeExecutionTriggeredFrom.RAG_PIPELINE_RUN,
                    )
                )

            # Set the user directly in g for preserve_flask_contexts
            g._login_user = account

            # Copy context for passing to pipeline generator
            context = contextvars.copy_context()

            # Direct execution without creating another thread
            # Since we're already in a thread pool, no need for nested threading
            from core.app.apps.pipeline.pipeline_generator import PipelineGenerator

            pipeline_generator = PipelineGenerator()
            # Using protected method intentionally for async execution
            with Session(db.engine, expire_on_commit=False) as session:
                pipeline_generator._generate(  # type: ignore[attr-defined]
                    session=session,
                    flask_app=flask_app,
                    context=context,
                    pipeline=pipeline,
                    workflow_id=workflow_id,
                    user=account,
                    application_generate_entity=entity,
                    invoke_from=InvokeFrom.PUBLISHED_PIPELINE,
                    workflow_execution_repository=workflow_execution_repository,
                    workflow_node_execution_repository=workflow_node_execution_repository,
                    streaming=streaming,
                    workflow_thread_pool_id=workflow_thread_pool_id,
                )
        except Exception:
            logging.exception("Error in priority pipeline task")
            raise
