"use strict";

const LOADING_MESSAGES = [
  "Connecting to repository…",
  "Loading the latest review…",
  "Syncing findings…",
  "Preparing your report…",
];

const MESSAGE_INTERVAL = 500;
const REFRESH_INTERVAL = 5000;
const API_URL = "/api/latest-review";

const body = document.body;
const loadingScreen = document.querySelector("#loading-screen");
const loaderStatus = document.querySelector("#loader-status");
const rerunButton = document.querySelector("#rerun-button");
const filterButtons = document.querySelectorAll(".filter");
const findingsBody = document.querySelector("#findings-body");
const emptyState = document.querySelector("#empty-state");

let messageTimer;
let isFetching = false;

function startLoading() {
  let messageIndex = 0;

  window.clearInterval(messageTimer);
  body.classList.remove("dashboard-ready");
  body.classList.add("is-loading");
  loadingScreen.setAttribute("aria-hidden", "false");
  loaderStatus.textContent = LOADING_MESSAGES[messageIndex];

  messageTimer = window.setInterval(() => {
    messageIndex = Math.min(messageIndex + 1, LOADING_MESSAGES.length - 1);
    loaderStatus.textContent = LOADING_MESSAGES[messageIndex];
  }, MESSAGE_INTERVAL);
}

function finishLoading() {
  window.clearInterval(messageTimer);
  body.classList.remove("is-loading");
  body.classList.add("dashboard-ready");
  loadingScreen.setAttribute("aria-hidden", "true");
}

function setText(selector, value) {
  const element = document.querySelector(selector);
  if (element) element.textContent = value;
}

function normalizeFindings(value) {
  return Array.isArray(value) ? value : [];
}

function displaySeverity(severity) {
  const normalized = String(severity || "info").toLowerCase();

  if (["critical", "error", "high"].includes(normalized)) {
    return { key: "medium", label: "Critical" };
  }
  if (["suggestion", "warning", "medium", "low"].includes(normalized)) {
    return {
      key: "low",
      label: normalized === "suggestion" ? "Suggestion" : "Warning",
    };
  }
  return {
    key: "info",
    label: normalized === "praise" ? "Praise" : "Info",
  };
}

function filterFindings(severity) {
  let visibleCount = 0;

  findingsBody.querySelectorAll("tr").forEach((row) => {
    const isVisible = severity === "all" || row.dataset.severity === severity;
    row.hidden = !isVisible;
    if (isVisible) visibleCount += 1;
  });

  emptyState.hidden = visibleCount !== 0;
}

function createFindingRow(finding, source) {
  const severity = displaySeverity(finding.severity);
  const row = document.createElement("tr");
  row.dataset.severity = severity.key;

  const severityCell = document.createElement("td");
  const severityBadge = document.createElement("span");
  severityBadge.className = `severity severity--${severity.key}`;
  severityBadge.append(
    document.createElement("i"),
    document.createTextNode(` ${severity.label}`),
  );
  severityCell.append(severityBadge);

  const fileCell = document.createElement("td");
  const fileName = document.createElement("code");
  fileName.textContent = finding.file || "unknown";
  fileCell.append(fileName);

  const lineCell = document.createElement("td");
  const lineNumber = document.createElement("span");
  lineNumber.className = "line-number";
  lineNumber.textContent = finding.line ?? "—";
  lineCell.append(lineNumber);

  const issueCell = document.createElement("td");
  issueCell.textContent =
    source === "static"
      ? finding.message || "Static analysis finding"
      : finding.issue || "AI finding";

  const suggestionCell = document.createElement("td");
  if (source === "static") {
    const tool = finding.tool || "Static analyzer";
    const rule = finding.rule ? ` (${finding.rule})` : "";
    suggestionCell.textContent = `Reported by ${tool}${rule}.`;
  } else {
    suggestionCell.textContent =
      finding.suggestion || "Review this finding.";
  }

  row.append(
    severityCell,
    fileCell,
    lineCell,
    issueCell,
    suggestionCell,
  );
  return row;
}

function renderFindings(staticFindings, aiFindings) {
  const fragment = document.createDocumentFragment();

  staticFindings.forEach((finding) => {
    fragment.append(createFindingRow(finding, "static"));
  });
  aiFindings.forEach((finding) => {
    fragment.append(createFindingRow(finding, "ai"));
  });
  findingsBody.replaceChildren(fragment);

  const total = staticFindings.length + aiFindings.length;
  setText("#findings-count", total);
  emptyState.textContent = total
    ? "No findings match this filter."
    : "No findings were reported.";

  const activeFilter =
    document.querySelector(".filter.is-active")?.dataset.filter || "all";
  filterFindings(activeFilter);
}

function renderReview(data) {
  const staticFindings = normalizeFindings(data.static_findings);
  const aiFindings = normalizeFindings(data.ai_findings);
  const hasReview = Boolean(data.repo && data.pr_number);
  const filesReviewed = Number(data.files_reviewed) || 0;
  const status = data.status || "Waiting for review";

  setText("#repo-name", data.repo || "—");
  setText("#pr-number", hasReview ? `#${data.pr_number}` : "—");
  setText("#pr-meta", hasReview ? "Latest" : "Waiting");
  setText("#review-status", status);
  setText("#review-status-meta", hasReview ? "Live" : "Idle");
  setText("#files-reviewed", filesReviewed);
  setText("#files-total", filesReviewed);
  setText("#files-percent", hasReview ? "100%" : "0%");
  setText("#static-total", staticFindings.length);
  setText("#static-trend", `${staticFindings.length} total`);
  setText("#ai-total", aiFindings.length);
  setText("#ai-trend", `${aiFindings.length} total`);
  setText("#coverage-title", hasReview ? "Full coverage" : "Waiting for review");
  setText(
    "#coverage-details",
    `${filesReviewed} ${filesReviewed === 1 ? "file" : "files"} analyzed`,
  );
  setText("#review-time", hasReview ? "Live" : "Waiting");
  setText("#review-completed", hasReview ? "Latest review" : "No review yet");
  setText("#service-status-text", "Backend connected");
  setText(
    "#footer-status",
    hasReview ? "Analysis complete" : "Waiting for analysis",
  );

  const now = new Date();
  setText(
    "#review-updated",
    hasReview
      ? now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
      : "—",
  );

  const summaryParagraph = document.createElement("p");
  summaryParagraph.textContent =
    data.summary || "Waiting for the first completed pull request review.";
  document.querySelector("#summary-copy").replaceChildren(summaryParagraph);

  const githubButton = document.querySelector("#github-button");
  githubButton.href = hasReview
    ? `https://github.com/${data.repo}/pull/${data.pr_number}`
    : "https://github.com/";

  const criticalCount = aiFindings.filter(
    (finding) => displaySeverity(finding.severity).key === "medium",
  ).length;
  const suggestionCount = aiFindings.filter(
    (finding) => displaySeverity(finding.severity).key === "low",
  ).length;
  const infoCount = Math.max(
    aiFindings.length - criticalCount - suggestionCount,
    0,
  );
  const aiTotal = aiFindings.length || 1;

  document.querySelector("#distribution-medium").style.width =
    `${(criticalCount / aiTotal) * 100}%`;
  document.querySelector("#distribution-low").style.width =
    `${(suggestionCount / aiTotal) * 100}%`;
  document.querySelector("#distribution-info").style.width =
    `${(infoCount / aiTotal) * 100}%`;
  setText("#medium-label", `${criticalCount} critical`);
  setText("#low-label", `${suggestionCount} suggestions`);

  const currentBar = document.querySelector(".mini-bars .is-current");
  if (currentBar) {
    currentBar.style.height =
      `${Math.min(100, Math.max(8, staticFindings.length * 10))}%`;
  }

  document.querySelector("#confidence-track span").style.width =
    hasReview ? "100%" : "0%";
  setText("#confidence-value", hasReview ? "Live" : "Waiting");

  renderFindings(staticFindings, aiFindings);
}

async function loadLatestReview({ showLoader = false } = {}) {
  if (isFetching) return;
  isFetching = true;

  if (showLoader) startLoading();

  try {
    const response = await fetch(API_URL, {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });

    if (!response.ok) {
      throw new Error(
        `Latest review request failed with status ${response.status}`,
      );
    }

    renderReview(await response.json());
  } catch (error) {
    console.error("Unable to load the latest CodeSage review:", error);
    setText("#service-status-text", "Backend unavailable");
    setText("#footer-status", "Waiting for backend");
  } finally {
    isFetching = false;
    if (showLoader) finishLoading();
  }
}

filterButtons.forEach((button) => {
  button.addEventListener("click", () => {
    filterButtons.forEach((item) => {
      item.classList.toggle("is-active", item === button);
    });

    filterFindings(button.dataset.filter);
  });
});

rerunButton.addEventListener("click", () => {
  rerunButton.classList.add("is-spinning");
  loadLatestReview({ showLoader: true });
  window.setTimeout(() => rerunButton.classList.remove("is-spinning"), 850);
});

window.addEventListener("load", () => {
  loadLatestReview({ showLoader: true });
  window.setInterval(loadLatestReview, REFRESH_INTERVAL);
});
