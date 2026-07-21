// Progressive form wiring for HiggsHole.
//
// HTMX handles the partial updates on these pages — the model-controls panel,
// the live estimate, the job feed. The two *primary* forms, though, submit a
// typed JSON body to the REST API and then navigate on success, which plain
// HTMX form-encoding does not express. Without this file their submit buttons
// fall back to the browser default: a GET that puts every field — including
// the API key — into the URL and never reaches the API. So these two forms are
// wired here with fetch(), against the same endpoints the MCP server calls.
"use strict";

// Build a JSON object from a form's fields.
//
// Empty values are omitted, so a blank optional field stays absent rather than
// overriding a provider default — and, crucially, a blank API-key field never
// clears a saved key. Repeated field names collapse into an array, which is
// how the reference-image slots submit. Type coercion (seed and duration to
// int, generate_audio to bool) is left to the API's Pydantic models, which
// accept the string forms.
function formJson(form, skip) {
  const out = {};
  new FormData(form).forEach(function (value, key) {
    if (skip && skip.indexOf(key) !== -1) return;
    if (typeof value === "string" && value.trim() === "") return;
    if (key in out) {
      if (!Array.isArray(out[key])) out[key] = [out[key]];
      out[key].push(value);
    } else {
      out[key] = value;
    }
  });
  return out;
}

async function errorMessage(response) {
  try {
    const body = await response.json();
    return body.message || body.error || "HTTP " + response.status;
  } catch (e) {
    return "HTTP " + response.status;
  }
}

function notify(form, text, kind) {
  let box = form.querySelector(".form-status");
  if (!box) {
    box = document.createElement("p");
    box.className = "form-status";
    form.appendChild(box);
  }
  box.textContent = text;
  box.dataset.kind = kind || "info";
}

function wireSettings() {
  const form = document.getElementById("settings-form");
  if (!form) return;
  form.addEventListener("submit", async function (evt) {
    evt.preventDefault();
    notify(form, "Saving…", "info");
    try {
      const response = await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(formJson(form)),
      });
      if (!response.ok) {
        notify(form, await errorMessage(response), "error");
        return;
      }
      // A freshly saved key is useless until the catalogue is fetched with it —
      // otherwise the model picker stays empty and the app looks broken. Pull
      // the catalogue now so models are ready on the very next screen. Best
      // effort: a refresh failure still leaves the key saved and the manual
      // "Refresh catalogue" button available.
      notify(form, "Saved. Loading models…", "info");
      try {
        await fetch("/api/settings/catalog/refresh", { method: "POST" });
      } catch (e) {
        // ignore — the reload will surface any catalogue error
      }
      // Reload so the newly masked key and the refreshed catalogue status show.
      window.location.reload();
    } catch (e) {
      notify(form, String(e), "error");
    }
  });
}

function wireCreate() {
  const form = document.getElementById("create-form");
  if (!form) return;
  form.addEventListener("submit", async function (evt) {
    evt.preventDefault();
    const kind = new FormData(form).get("kind") || "image";
    const payload = formJson(form, ["kind"]);
    const button = form.querySelector("button[type=submit]");
    if (button) button.disabled = true;
    notify(form, "Submitting…", "info");
    try {
      const response = await fetch("/api/generate/" + kind, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        notify(form, await errorMessage(response), "error");
        if (button) button.disabled = false;
        return;
      }
      const body = await response.json();
      // An image is finished on return, so go straight to its detail page; a
      // video is a job in flight, so go to the feed that tracks it.
      window.location.href =
        kind === "video" ? "/jobs" : "/library/" + body.id;
    } catch (e) {
      notify(form, String(e), "error");
      if (button) button.disabled = false;
    }
  });
}

document.addEventListener("DOMContentLoaded", function () {
  wireSettings();
  wireCreate();
});
