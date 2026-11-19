import { useEffect, useId, useState, type KeyboardEvent } from "react";
import {
  DEFAULT_SHORTCUTS,
  SHORTCUT_ACTIONS,
  SHORTCUT_LABELS,
  acceleratorFromKeyEvent,
  type ShortcutAction,
  type ShortcutConfig,
} from "../../../shared/shortcuts";
import { useDesktopStore } from "../store";

/**
 * Keyboard shortcut settings (M-4, D-69). The user can turn shortcuts off, change each one by
 * pressing the new combination in its box, or reset to the defaults. Validation, saving and
 * registration all happen in the main process; this component only edits a draft.
 *
 * None of these shortcuts starts listening or capturing: they show, hide or move to a panel.
 */
export function ShortcutSettings() {
  const state = useDesktopStore((s) => s.shortcuts);
  const busy = useDesktopStore((s) => s.shortcutsBusy);
  const notice = useDesktopStore((s) => s.shortcutsNotice);
  const load = useDesktopStore((s) => s.loadShortcuts);
  const save = useDesktopStore((s) => s.saveShortcuts);
  const [draft, setDraft] = useState<ShortcutConfig | null>(null);
  const [saved, setSaved] = useState(false);
  const helpId = useId();

  useEffect(() => {
    void load();
  }, [load]);
  useEffect(() => {
    if (state) setDraft(structuredClone(state.config));
  }, [state]);

  if (!state || !draft) return null;

  const changed = JSON.stringify(draft) !== JSON.stringify(state.config);
  const setBinding = (action: ShortcutAction, value: string) => {
    setSaved(false);
    setDraft({ ...draft, bindings: { ...draft.bindings, [action]: value } });
  };
  const onKeyDown = (action: ShortcutAction) => (event: KeyboardEvent<HTMLInputElement>) => {
    // Tab and Escape keep their normal meaning, so the field never traps keyboard users.
    if (event.key === "Tab" || event.key === "Escape") return;
    event.preventDefault();
    const accelerator = acceleratorFromKeyEvent(event);
    if (accelerator) setBinding(action, accelerator);
  };

  return (
    <section aria-labelledby={`${helpId}-title`} className="settings-section">
      <h2 id={`${helpId}-title`}>Keyboard shortcuts</h2>
      <p className="settings-help" id={helpId}>
        Shortcuts work from any application. They only show, hide or move to a panel: they never start
        listening or take a screenshot. Each one needs Shift plus Ctrl or Alt plus one letter, number or
        function key. To change one, select its box and press the new keys; Tab moves on.
      </p>
      <label className="toggle-row">
        <span>
          <span className="toggle-label">Use keyboard shortcuts</span>
          <span className="toggle-description">Turn all shortcuts on or off.</span>
        </span>
        <input
          checked={draft.enabled}
          onChange={(event) => {
            setSaved(false);
            setDraft({ ...draft, enabled: event.target.checked });
          }}
          type="checkbox"
        />
      </label>

      <ul className="shortcut-list">
        {SHORTCUT_ACTIONS.map((action) => {
          const status = state.status[action];
          const inputId = `${helpId}-${action}`;
          return (
            <li className="shortcut-row" key={action}>
              <label htmlFor={inputId}>
                <span className="toggle-label">{SHORTCUT_LABELS[action].label}</span>
                <span className="toggle-description">{SHORTCUT_LABELS[action].description}</span>
              </label>
              <div className="shortcut-controls">
                <input
                  aria-describedby={`${inputId}-status ${helpId}`}
                  className="field-control shortcut-input"
                  disabled={!draft.enabled}
                  id={inputId}
                  onChange={() => undefined}
                  onKeyDown={onKeyDown(action)}
                  readOnly
                  value={draft.bindings[action]}
                />
                <button
                  className="text-button"
                  disabled={!draft.enabled || draft.bindings[action] === DEFAULT_SHORTCUTS[action]}
                  onClick={() => setBinding(action, DEFAULT_SHORTCUTS[action])}
                  type="button"
                >
                  Reset
                </button>
              </div>
              <p
                className={status.kind === "active" || status.kind === "disabled" ? "settings-help" : "inline-error"}
                id={`${inputId}-status`}
              >
                {changed ? "Not saved yet." : status.message}
              </p>
            </li>
          );
        })}
      </ul>

      {notice && (
        <p className="inline-error" role="alert">
          {notice}
        </p>
      )}
      <div className="capture-actions">
        <button
          className="button button-primary"
          disabled={busy || !changed}
          onClick={() => void save(draft).then((ok) => setSaved(ok))}
          type="button"
        >
          {busy ? "Saving…" : "Save shortcuts"}
        </button>
        <button
          className="button button-secondary"
          disabled={busy}
          onClick={() => {
            setSaved(false);
            setDraft({ enabled: true, bindings: { ...DEFAULT_SHORTCUTS } });
          }}
          type="button"
        >
          Reset all
        </button>
      </div>
      {saved && !changed && (
        <p className="settings-help" role="status">
          Saved.
        </p>
      )}
    </section>
  );
}
