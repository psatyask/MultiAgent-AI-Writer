const runForm = document.getElementById("runForm");
const topicInput = document.getElementById("topicInput");
const runButton = document.getElementById("runButton");
const cancelButton = document.getElementById("cancelButton");
const newRunButton = document.getElementById("newRunButton");

const timeline = document.getElementById("timeline");
const activityEmpty = document.getElementById("activityEmpty");

const planList = document.getElementById("planList");
const planEmpty = document.getElementById("planEmpty");
const planMeta = document.getElementById("planMeta");
const taskCount = document.getElementById("taskCount");

const progressRing = document.getElementById("progressRing");
const progressText = document.getElementById("progressText");
const runBadge = document.getElementById("runBadge");

const articlePreview = document.getElementById("articlePreview");
const markdownOutput = document.getElementById("markdownOutput");

const previewTab = document.getElementById("previewTab");
const markdownTab = document.getElementById("markdownTab");

const copyButton = document.getElementById("copyButton");
const downloadButton = document.getElementById("downloadButton");
const downloadImagesButton = document.getElementById("downloadImagesButton");
const imagesLabel = document.getElementById("imagesLabel");

const healthDot = document.getElementById("healthDot");
const healthText = document.getElementById("healthText");

const toast = document.getElementById("toast");
const toastBody = document.getElementById("toastBody");
const sidebarMenu = document.getElementById("sidebarMenu");

let abortController = null;
let finalMarkdown = "";
let totalTasks = 0;
let completedTasks = 0;

const stageProgress = {
    router: 10,
    research: 25,
    orchestrator: 35,
    workers: 75,
    reducer: 100,
};

const markerIcons = {
    pending: "bi-circle",
    running: "bi-arrow-repeat spin",
    completed: "bi-check-circle-fill",
    failed: "bi-x-circle-fill",
    warning: "bi-exclamation-triangle-fill",
};

const emptyResultHtml = `
    <div class="empty-state">
        <div class="empty-icon"><i class="bi bi-file-earmark-richtext"></i></div>
        <h3>Your article will appear here</h3>
        <p>The final result appears after every section is completed.</p>
    </div>
`;


function escapeHtml(value) {
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}


function showToast(message) {
    toastBody.textContent = message;

    if (window.bootstrap) {
        bootstrap.Toast
            .getOrCreateInstance(toast, { delay: 2600 })
            .show();
    }
}


function updateProgress(value) {
    const progress = Math.max(0, Math.min(100, Math.round(value)));

    progressRing.style.setProperty("--progress", progress);
    progressText.textContent = `${progress}%`;
}


function setMarker(element, status) {
    const icon = element.querySelector(".timeline-marker i");

    if (icon) {
        icon.className = `bi ${markerIcons[status] || markerIcons.pending}`;
    }
}


function markWorkflow(stageId, status) {
    const item = document.querySelector(`.workflow-item[data-step="${stageId}"]`);

    if (!item) {
        return;
    }

    item.classList.remove("running", "done");

    if (status === "running") {
        item.classList.add("running");
    } else if (status === "completed") {
        item.classList.add("done");
    }
}


function resetInterface() {
    finalMarkdown = "";
    totalTasks = 0;
    completedTasks = 0;

    timeline.innerHTML = "";
    planList.innerHTML = "";

    activityEmpty.hidden = false;
    planEmpty.hidden = false;

    timeline.appendChild(activityEmpty);
    planList.appendChild(planEmpty);

    planMeta.innerHTML = "";
    planMeta.hidden = true;

    taskCount.textContent = "0 tasks";

    articlePreview.innerHTML = emptyResultHtml;

    markdownOutput.textContent = "";
    markdownOutput.hidden = true;
    articlePreview.hidden = false;

    previewTab.classList.add("active");
    markdownTab.classList.remove("active");

    copyButton.disabled = true;

    downloadButton.href = "#";
    downloadButton.classList.add("disabled");
    downloadButton.setAttribute("aria-disabled", "true");

    downloadImagesButton.href = "#";
    downloadImagesButton.classList.add("disabled");
    downloadImagesButton.setAttribute("aria-disabled", "true");
    imagesLabel.textContent = "Download images";

    document
        .querySelectorAll(".workflow-item")
        .forEach(item => item.classList.remove("running", "done"));

    runBadge.textContent = "Ready";
    runBadge.className = "run-badge";

    updateProgress(0);
}


function setRunningState(isRunning) {
    topicInput.disabled = isRunning;
    runButton.disabled = isRunning;

    cancelButton.hidden = !isRunning;

    const label = runButton.querySelector(".run-label");
    const icon = runButton.querySelector(".run-icon");

    if (label) {
        label.textContent = isRunning ? "Running..." : "Run agents";
    }

    if (icon) {
        icon.className = isRunning
            ? "bi bi-arrow-repeat spin run-icon"
            : "bi bi-arrow-right run-icon";
    }

    if (isRunning) {
        runBadge.textContent = "Running";
        runBadge.className = "run-badge running";
    }
}


function getStageElement(stageId) {
    return document.querySelector(`[data-stage-id="${stageId}"]`);
}


function createStageElement(stageId) {
    const element = document.createElement("div");

    element.className = "timeline-item";
    element.dataset.stageId = stageId;

    element.innerHTML = `
        <div class="timeline-marker">
            <i class="bi bi-circle"></i>
        </div>

        <div class="timeline-content">
            <div class="timeline-title"></div>
            <div class="timeline-detail"></div>
        </div>
    `;

    timeline.appendChild(element);

    return element;
}


function updateStage(event) {
    activityEmpty.hidden = true;

    let element = getStageElement(event.id);

    if (!element) {
        element = createStageElement(event.id);
    }

    const status = event.status || "running";

    element.classList.remove("running", "completed", "failed", "warning");
    element.classList.add(status);

    setMarker(element, status);

    element.querySelector(".timeline-title").textContent = event.label;
    element.querySelector(".timeline-detail").textContent = event.detail || "";

    markWorkflow(event.id, status);

    if (status === "completed" && stageProgress[event.id] !== undefined) {
        updateProgress(stageProgress[event.id]);
    }
}


function addSubstage(event) {
    activityEmpty.hidden = true;

    const status = event.status || "completed";

    let element = document.querySelector(`[data-substage-id="${event.id}"]`);

    if (!element) {
        element = document.createElement("div");

        element.className = "timeline-item substage";
        element.dataset.substageId = event.id;

        element.innerHTML = `
            <div class="timeline-marker">
                <i class="bi bi-circle"></i>
            </div>

            <div class="timeline-content">
                <div class="timeline-title"></div>
                <div class="timeline-detail"></div>
            </div>
        `;

        timeline.appendChild(element);
    }

    element.classList.remove("running", "completed", "failed", "warning");
    element.classList.add(status);

    setMarker(element, status);

    element.querySelector(".timeline-title").textContent = event.label;
    element.querySelector(".timeline-detail").textContent =
        status === "running" ? "In progress" : "Completed";
}


function showWarning(message) {
    activityEmpty.hidden = true;

    const element = document.createElement("div");

    element.className = "timeline-item warning";

    element.innerHTML = `
        <div class="timeline-marker">
            <i class="bi ${markerIcons.warning}"></i>
        </div>

        <div class="timeline-content">
            <div class="timeline-title">Heads up</div>
            <div class="timeline-detail">${escapeHtml(message)}</div>
        </div>
    `;

    timeline.appendChild(element);

    showToast("Some images could not be generated");
}


function showRouting(event) {
    const modeLabels = {
        closed_book: "Evergreen topic",
        hybrid: "Research-assisted topic",
        open_book: "Current information topic",
    };

    const detail = modeLabels[event.mode] || event.mode;

    const element = getStageElement("router");

    if (element) {
        element.querySelector(".timeline-detail").textContent =
            `${detail}. Research: ${event.needs_research ? "required" : "not required"}.`;
    }

    if (Array.isArray(event.queries) && event.queries.length) {
        const queriesElement = document.createElement("div");

        queriesElement.className = "query-list";

        queriesElement.innerHTML = event.queries
            .map(query => `<span>${escapeHtml(query)}</span>`)
            .join("");

        if (element) {
            element
                .querySelector(".timeline-content")
                .appendChild(queriesElement);
        }
    }
}


function showResearch(event) {
    if (!event.evidence?.length) {
        return;
    }

    const stage = getStageElement("research");

    if (!stage) {
        return;
    }

    const sources = document.createElement("div");
    sources.className = "source-list";

    sources.innerHTML = event.evidence
        .slice(0, 5)
        .map(source => `
            <a
                href="${escapeHtml(source.url)}"
                target="_blank"
                rel="noopener noreferrer"
            >
                <i class="bi bi-link-45deg"></i>
                ${escapeHtml(source.title || source.url)}
            </a>
        `)
        .join("");

    stage
        .querySelector(".timeline-content")
        .appendChild(sources);
}


function showPlan(plan) {
    planEmpty.hidden = true;
    planList.innerHTML = "";

    const tasks = plan.tasks || [];

    totalTasks = tasks.length;
    completedTasks = 0;

    taskCount.textContent =
        `${tasks.length} ${tasks.length === 1 ? "task" : "tasks"}`;

    planMeta.hidden = false;

    planMeta.innerHTML = `
        <div class="col-12 col-sm-4">
            <div class="meta-box">
                <span><i class="bi bi-people"></i> Audience</span>
                <strong>${escapeHtml(plan.audience || "Developers")}</strong>
            </div>
        </div>

        <div class="col-12 col-sm-4">
            <div class="meta-box">
                <span><i class="bi bi-layout-text-window"></i> Format</span>
                <strong>
                    ${escapeHtml((plan.blog_kind || "explainer").replaceAll("_", " "))}
                </strong>
            </div>
        </div>

        <div class="col-12 col-sm-4">
            <div class="meta-box">
                <span><i class="bi bi-chat-quote"></i> Tone</span>
                <strong>${escapeHtml(plan.tone || "Technical")}</strong>
            </div>
        </div>
    `;

    tasks.forEach((task, index) => {
        const item = document.createElement("article");

        item.className = "plan-item";
        item.dataset.taskId = String(task.id);

        const tags = [];

        if (task.requires_research) {
            tags.push("Research");
        }

        if (task.requires_citations) {
            tags.push("Citations");
        }

        if (task.requires_code) {
            tags.push("Code");
        }

        item.innerHTML = `
            <div class="plan-number">
                ${String(index + 1).padStart(2, "0")}
            </div>

            <div class="plan-content">
                <div class="plan-title-row">
                    <h3>${escapeHtml(task.title)}</h3>

                    <span class="plan-status">
                        <i class="bi bi-hourglass-split"></i> Waiting
                    </span>
                </div>

                <p>${escapeHtml(task.goal)}</p>

                <div class="plan-tags">
                    <span>${escapeHtml(task.target_words)} words</span>

                    ${tags.map(tag => `<span>${escapeHtml(tag)}</span>`).join("")}
                </div>

                <div class="task-progress">
                    <span></span>
                </div>
            </div>
        `;

        planList.appendChild(item);
    });
}


function completeSection(event) {
    const taskElement = document.querySelector(
        `[data-task-id="${event.task_id}"]`,
    );

    if (taskElement) {
        taskElement.classList.add("completed");

        taskElement.querySelector(".plan-status").innerHTML =
            '<i class="bi bi-check2-circle"></i> Completed';

        taskElement.querySelector(".task-progress span").style.width = "100%";
    }

    completedTasks = event.completed || (completedTasks + 1);
    totalTasks = event.total || totalTasks;

    const workerProgress = totalTasks
        ? 35 + (completedTasks / totalTasks) * 40
        : 35;

    updateProgress(workerProgress);

    const workerStage = getStageElement("workers");

    if (workerStage) {
        workerStage.querySelector(".timeline-detail").textContent =
            `${completedTasks} of ${totalTasks} sections completed.`;
    }
}


function enhanceArticle() {
    // Make tables scroll sideways on small screens.
    articlePreview.querySelectorAll("table").forEach(table => {
        table.classList.add("table", "table-sm", "align-middle");

        const wrapper = document.createElement("div");
        wrapper.className = "table-responsive";

        table.parentNode.insertBefore(wrapper, table);
        wrapper.appendChild(table);
    });

    // Turn "image + *caption*" paragraphs into proper figures.
    articlePreview.querySelectorAll("img").forEach(image => {
        image.loading = "lazy";
        image.classList.add("img-fluid");

        image.addEventListener("error", () => {
            image.alt = image.alt || "Image could not be loaded";
        });

        const parent = image.parentElement;

        if (!parent || parent.tagName !== "P") {
            return;
        }

        const figure = document.createElement("figure");
        figure.className = "article-figure";

        parent.parentNode.insertBefore(figure, parent);
        figure.appendChild(image);

        const caption = parent.querySelector("em");

        if (caption) {
            const figcaption = document.createElement("figcaption");
            figcaption.textContent = caption.textContent;

            figure.appendChild(figcaption);
            caption.remove();
        }

        if (!parent.textContent.trim() && !parent.querySelector("img")) {
            parent.remove();
        }
    });
}


function displayFinalResult(event) {
    finalMarkdown = event.markdown || "";

    markdownOutput.textContent = finalMarkdown;

    const rendered = marked.parse(finalMarkdown);

    articlePreview.innerHTML = DOMPurify.sanitize(rendered);

    enhanceArticle();

    copyButton.disabled = false;

    downloadButton.href = event.download_url;
    downloadButton.classList.remove("disabled");
    downloadButton.setAttribute("aria-disabled", "false");

    if (event.images_url) {
        downloadImagesButton.href = event.images_url;
        downloadImagesButton.classList.remove("disabled");
        downloadImagesButton.setAttribute("aria-disabled", "false");
        imagesLabel.textContent = event.image_count > 0
            ? `Download images (${event.image_count})`
            : "Download images";
    }

    runBadge.textContent = "Completed";
    runBadge.className = "run-badge completed";

    updateProgress(100);

    document
        .getElementById("resultCard")
        .scrollIntoView({
            behavior: "smooth",
            block: "start",
        });
}


function failRunningItems() {
    document
        .querySelectorAll(".timeline-item.running")
        .forEach(element => {
            element.classList.remove("running");
            element.classList.add("failed");
            setMarker(element, "failed");
        });

    document
        .querySelectorAll(".workflow-item.running")
        .forEach(item => item.classList.remove("running"));
}


function displayError(message) {
    runBadge.textContent = "Failed";
    runBadge.className = "run-badge failed";

    failRunningItems();

    const errorItem = document.createElement("div");

    errorItem.className = "timeline-item failed";

    errorItem.innerHTML = `
        <div class="timeline-marker">
            <i class="bi ${markerIcons.failed}"></i>
        </div>

        <div class="timeline-content">
            <div class="timeline-title">Agent execution failed</div>
            <div class="timeline-detail">${escapeHtml(message)}</div>
        </div>
    `;

    timeline.appendChild(errorItem);

    showToast("Agent execution failed");
}


function handleEvent(event) {
    switch (event.type) {
        case "run_started":
            runBadge.textContent = "Running";
            break;

        case "stage":
            updateStage(event);
            break;

        case "substage":
            addSubstage(event);
            break;

        case "routing":
            showRouting(event);
            break;

        case "research_complete":
            showResearch(event);
            break;

        case "plan":
            showPlan(event.plan || {});
            break;

        case "section_complete":
            completeSection(event);
            break;

        case "images_planned":
            showToast(
                `${event.count} visual${event.count === 1 ? "" : "s"} planned`,
            );
            break;

        case "warning":
            showWarning(event.message || "Something needs attention.");
            break;

        case "final":
            displayFinalResult(event);
            break;

        case "error":
            displayError(event.message);
            break;

        case "done":
            setRunningState(false);
            break;
    }
}


function parseSSEChunk(buffer) {
    const events = buffer.split("\n\n");
    const remaining = events.pop() || "";

    events.forEach(block => {
        const dataLines = block
            .split("\n")
            .filter(line => line.startsWith("data:"))
            .map(line => line.slice(5).trim());

        if (!dataLines.length) {
            return;
        }

        const rawData = dataLines.join("\n");

        try {
            const event = JSON.parse(rawData);
            handleEvent(event);
        } catch (error) {
            console.error("Could not parse stream event:", rawData, error);
        }
    });

    return remaining;
}


async function executeAgent(topic) {
    abortController = new AbortController();

    const response = await fetch("/api/run", {
        method: "POST",

        headers: {
            "Content-Type": "application/json",
        },

        body: JSON.stringify({ topic }),

        signal: abortController.signal,
    });

    if (!response.ok) {
        const message = await response.text();

        throw new Error(message || "Could not start the agent.");
    }

    if (!response.body) {
        throw new Error("Streaming is not supported by this browser.");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();

    let buffer = "";

    while (true) {
        const { done, value } = await reader.read();

        if (done) {
            break;
        }

        buffer += decoder.decode(value, { stream: true });

        buffer = parseSSEChunk(buffer);
    }

    buffer += decoder.decode();

    if (buffer.trim()) {
        parseSSEChunk(`${buffer}\n\n`);
    }
}


function closeSidebar() {
    if (!window.bootstrap || !sidebarMenu) {
        return;
    }

    const instance = bootstrap.Offcanvas.getInstance(sidebarMenu);

    if (instance) {
        instance.hide();
    }
}


runForm.addEventListener("submit", async event => {
    event.preventDefault();

    const topic = topicInput.value.trim();

    if (topic.length < 3) {
        showToast("Enter a more detailed topic.");
        return;
    }

    resetInterface();
    setRunningState(true);

    activityEmpty.hidden = true;
    planEmpty.hidden = false;

    try {
        await executeAgent(topic);
    } catch (error) {
        if (error.name === "AbortError") {
            runBadge.textContent = "Stopped";
            runBadge.className = "run-badge stopped";

            failRunningItems();

            showToast("Generation stopped.");
        } else {
            displayError(error.message || "Unexpected error.");
        }
    } finally {
        setRunningState(false);
        abortController = null;
    }
});


cancelButton.addEventListener("click", () => {
    if (abortController) {
        abortController.abort();
    }
});


newRunButton.addEventListener("click", () => {
    if (abortController) {
        abortController.abort();
    }

    resetInterface();

    topicInput.disabled = false;
    topicInput.value = "";
    topicInput.focus();

    closeSidebar();
});


document
    .querySelectorAll("[data-topic]")
    .forEach(button => {
        button.addEventListener("click", () => {
            topicInput.value = button.dataset.topic || "";

            topicInput.focus();
        });
    });


previewTab.addEventListener("click", () => {
    previewTab.classList.add("active");
    markdownTab.classList.remove("active");

    articlePreview.hidden = false;
    markdownOutput.hidden = true;
});


markdownTab.addEventListener("click", () => {
    markdownTab.classList.add("active");
    previewTab.classList.remove("active");

    markdownOutput.hidden = false;
    articlePreview.hidden = true;
});


copyButton.addEventListener("click", async () => {
    if (!finalMarkdown) {
        return;
    }

    try {
        await navigator.clipboard.writeText(finalMarkdown);

        showToast("Markdown copied.");
    } catch {
        showToast("Could not copy. Use the Markdown tab instead.");
    }
});


downloadButton.addEventListener("click", event => {
    if (downloadButton.classList.contains("disabled")) {
        event.preventDefault();
    }
});


downloadImagesButton.addEventListener("click", event => {
    if (downloadImagesButton.classList.contains("disabled")) {
        event.preventDefault();
    }
});


async function checkHealth() {
    try {
        const response = await fetch("/api/health");

        if (!response.ok) {
            throw new Error("Server unavailable");
        }

        healthDot.classList.add("online");
        healthText.textContent = "Server online";
    } catch {
        healthDot.classList.remove("online");
        healthText.textContent = "Server offline";
    }
}


resetInterface();
checkHealth();
