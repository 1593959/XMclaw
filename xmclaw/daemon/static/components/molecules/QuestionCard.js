// XMclaw — QuestionCard (B-92)
//
// Renders the agent's mid-turn ask_user_question tool call as an
// interactive card in the chat transcript. While pending: clickable
// options + optional "Other → free text" + a Submit button (when
// multi-select). Once answered: read-only summary so the conversation
// keeps the audit trail.
//
// Lives in MessageBubble's render path: when message.kind === "question"
// the bubble renders this card instead of the normal text body.
// Submit calls back to onAnswerQuestion(questionId, value) which
// app.js wires to the WS frame {type: "answer_question", ...}.
//
// Same visual family as ToolCard / PhaseCard — bordered details-style
// container so it sits naturally alongside other tool cards in the
// transcript.

const { h } = window.__xmc.preact;
const { useState } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { Badge } from "../atoms/badge.js";

export function QuestionCard({ message, onAnswerQuestion }) {
  const q = message.question;
  if (!q) return null;
  const answered = message.status === "complete";
  const submitted = message.answer;

  // Selected option(s) for the in-progress card. Multi-select uses an
  // array; single-select stores the most-recent click.
  const [picked, setPicked] = useState(q.multi_select ? [] : "");
  const [otherText, setOtherText] = useState("");
  const [otherOpen, setOtherOpen] = useState(false);

  const onClickOption = (val) => {
    if (q.multi_select) {
      setPicked((cur) =>
        cur.includes(val) ? cur.filter((x) => x !== val) : cur.concat(val),
      );
      setOtherOpen(false);
    } else {
      // Single-select: clicking the option submits immediately —
      // saves the user a click. They can still pick "Other" first
      // and then submit the typed text below.
      setPicked(val);
      setOtherOpen(false);
      if (typeof onAnswerQuestion === "function") {
        onAnswerQuestion(q.id, val);
      }
    }
  };

  const onSubmit = () => {
    if (typeof onAnswerQuestion !== "function") return;
    if (otherOpen && otherText.trim()) {
      onAnswerQuestion(q.id, otherText.trim());
      return;
    }
    if (q.multi_select) {
      if (picked.length === 0) return;
      onAnswerQuestion(q.id, picked);
    } else if (picked) {
      onAnswerQuestion(q.id, picked);
    }
  };

  // Read-only summary after the answer landed.
  if (answered) {
    const display = Array.isArray(submitted)
      ? submitted.join(", ")
      : String(submitted == null ? "" : submitted);
    return html`
      <div class="xmc-questioncard xmc-questioncard--answered">
        <div class="xmc-questioncard__header">
          <strong>❓ ${q.question}</strong>
          <${Badge} tone="success">已回答</${Badge}>
        </div>
        <div class="xmc-questioncard__answer">
          <span class="xmc-questioncard__answer-label">你选择了：</span>
          <code>${display}</code>
        </div>
      </div>
    `;
  }

  // Active card.
  const showSubmit = q.multi_select || otherOpen;
  return html`
    <div class="xmc-questioncard xmc-questioncard--pending">
      <div class="xmc-questioncard__header">
        <strong>❓ ${q.question}</strong>
        <${Badge} tone="warn">等待回答</${Badge}>
      </div>
      <div class="xmc-questioncard__options">
        ${q.options.map((opt) => {
          const isPicked = q.multi_select
            ? picked.includes(opt.value)
            : picked === opt.value;
          return html`
            <button
              type="button"
              key=${opt.value}
              class=${"xmc-questioncard__option" + (isPicked ? " is-picked" : "")}
              onClick=${() => onClickOption(opt.value)}
            >
              <span class="xmc-questioncard__option-label">${opt.label}</span>
              ${opt.description
                ? html`<span class="xmc-questioncard__option-desc">${opt.description}</span>`
                : null}
            </button>
          `;
        })}
        ${q.allow_other ? html`
          <button
            type="button"
            class=${"xmc-questioncard__option xmc-questioncard__option--other" + (otherOpen ? " is-picked" : "")}
            onClick=${() => { setOtherOpen(true); if (!q.multi_select) setPicked(""); }}
          >
            <span class="xmc-questioncard__option-label">Other — 自定义回答</span>
          </button>
        ` : null}
      </div>
      ${otherOpen ? html`
        <div class="xmc-questioncard__other">
          <input
            type="text"
            class="xmc-h-input"
            value=${otherText}
            placeholder="输入你的回答…"
            onInput=${(e) => setOtherText(e.target.value)}
            onKeyDown=${(e) => { if (e.key === "Enter" && otherText.trim()) onSubmit(); }}
            autofocus
          />
        </div>
      ` : null}
      ${showSubmit ? html`
        <div class="xmc-questioncard__actions">
          <button
            type="button"
            class="xmc-h-btn xmc-h-btn--primary"
            onClick=${onSubmit}
            disabled=${
              otherOpen
                ? !otherText.trim()
                : (q.multi_select && picked.length === 0)
            }
          >
            提交回答
          </button>
        </div>
      ` : null}
    </div>
  `;
}
