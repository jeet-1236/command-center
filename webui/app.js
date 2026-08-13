/* commandcenter/webui/app.js — the Support surface's "add a note" panel and its confirmation dialog.
 *
 * Two behaviours here are contracts the rest of the desk relies on, and both are checked by a real browser
 * (samples/commandcenter/tests/test_ui_state.py, test_ui_layout.py) rather than by reading the source:
 *
 *   1. Submitting a note updates what the agent sees IMMEDIATELY. The desk works in one long-lived tab; a
 *      state that only appears after a reload is a state nobody sees, and agents re-submit because the
 *      first attempt looked like it did nothing.
 *   2. The confirmation dialog stays inside the viewport on a phone, with its confirm button reachable.
 *      A button pushed outside the viewport cannot be tapped, so the flow dead-ends on mobile.
 */

const state = {
  notes: [],
  submitting: false,
  lastSaved: null,
};

function render() {
  const list = document.getElementById("note-list");
  list.innerHTML = state.notes
    .map((n) => `<li class="note"><span class="note-body">${escapeHtml(n.body)}</span>
                 <span class="note-when">${n.when}</span></li>`)
    .join("");

  const status = document.getElementById("submit-status");
  status.textContent = state.lastSaved
    ? `Saved — ${state.notes.length} note${state.notes.length === 1 ? "" : "s"} on this ticket`
    : "No notes yet";
  status.dataset.saved = state.lastSaved ? "yes" : "no";

  const btn = document.getElementById("save-note");
  btn.disabled = state.submitting;
  btn.textContent = state.submitting ? "Saving…" : "Save note";
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* Add a note to the ticket. The list, the counter and the button all read from `state`, so anything that
 * changes `state` has to put the change on screen before it returns. */
function saveNote() {
  const input = document.getElementById("note-input");
  const body = input.value.trim();
  if (!body) return;
  state.submitting = true;
  render();

  // The desk's API call. Resolved immediately here so the test measures OUR rendering, not the network.
  Promise.resolve({ ok: true }).then(() => {
    state.notes.push({ body, when: "just now" });
    state.lastSaved = body;
    state.submitting = false;
    input.value = "";
    render();
  });
}

function openConfirm() {
  document.getElementById("confirm-modal").classList.add("open");
}
function closeConfirm() {
  document.getElementById("confirm-modal").classList.remove("open");
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("save-note").addEventListener("click", saveNote);
  document.getElementById("open-confirm").addEventListener("click", openConfirm);
  document.getElementById("confirm-cancel").addEventListener("click", closeConfirm);
  render();
});
