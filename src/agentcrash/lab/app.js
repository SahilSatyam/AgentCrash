/* AgentCrash Lab: multi-project run browser over /v1 APIs. No build step. */
"use strict";

async function getJSON(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json();
}

function esc(value) {
  return String(value).replace(/[&<>"]/g, (c) => `&#${c.charCodeAt(0)};`);
}

async function listRuns() {
  const project = document.getElementById("project").value.trim();
  const data = await getJSON(`/v1/runs?project=${encodeURIComponent(project)}`);
  const ul = document.getElementById("runs");
  ul.innerHTML = "";
  for (const run of data.runs) {
    const li = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `${run.workflow} — ${run.run_id} (${run.events} events)`;
    button.addEventListener("click", () => showRun(run.run_id));
    li.appendChild(button);
    ul.appendChild(li);
  }
  if (!data.runs.length) ul.innerHTML = "<li>No runs for this project yet.</li>";
}

function eventRow(event) {
  const fault = event.fault ? ` <strong>FAULT ${esc(event.fault.kind || "")}</strong>` : "";
  const tool = event.tool ? ` tool=${esc(event.tool)}` : "";
  return `<tr><td>${event.seq}</td><td>${esc(event.kind)}${tool}</td>` +
    `<td>${esc(event.step || "")}</td><td>${esc(event.status)}</td><td>${fault}</td></tr>`;
}

async function showRun(runId) {
  const detail = await getJSON(`/v1/runs/${encodeURIComponent(runId)}`);
  const verdict = detail.evaluation
    ? `<p>Verdict: <strong>${esc(detail.evaluation.verdict || "n/a")}</strong></p>` +
      "<ul>" + detail.evaluation.map((v) =>
        `<li>${esc(v.id)}: ${v.passed ? "PASS" : "FAIL"} — ${esc(v.evidence)}</li>`).join("") + "</ul>"
    : "<p>No evaluation submitted for this run.</p>";
  const faults = detail.faults.length
    ? `<p>Faults: ${detail.faults.map((f) => esc(f.kind || "?")).join(", ")}</p>`
    : "<p>No faults injected.</p>";
  document.getElementById("detail").innerHTML =
    `<h3>${esc(detail.workflow)} — ${esc(detail.run_id)}</h3>` +
    `<p>Project: ${esc(detail.project)}</p>${faults}${verdict}` +
    `<table><thead><tr><th>seq</th><th>kind</th><th>step</th>` +
    `<th>status</th><th>fault</th></tr></thead><tbody>` +
    detail.events.map(eventRow).join("") + "</tbody></table>";
}

document.getElementById("refresh").addEventListener("click", () => {
  listRuns().catch((err) => {
    document.getElementById("runs").innerHTML = `<li>Error: ${esc(err.message)}</li>`;
  });
});
listRuns().catch(() => {});
