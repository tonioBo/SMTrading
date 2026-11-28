/**
 * What the app shows: the selection made in the portal (screen, rotation), which the
 * remote can change locally until the portal changes it again (a new selection resets the
 * view: «la scelta fatta dal portale continua a valere»).
 */
import type { TvScreenKey, TvSelection } from "@/api/tvTypes";
import type { KeyAction } from "@/tvapp/keys";

export interface ViewState {
  screen: TvScreenKey;
  rotating: boolean;
  paused: boolean;
}

export const NOTES = {
  paused: "Rotazione in pausa",
  resumed: "Rotazione ripresa",
  noRotation: "Rotazione spenta: premi il tasto blu per avviarla",
  rotationOn: "Rotazione attiva",
  rotationOff: "Rotazione spenta",
  flowNotAllowed: "Il Fiume dei flussi è solo per gli amministratori",
  oneScreen: "C'è una sola schermata",
} as const;

type SelectionPart = Pick<TvSelection, "screen" | "rotation" | "screens">;

/** the identity of a portal choice: when it changes, the view starts again from it */
export const selectionKey = (sel: SelectionPart & Pick<TvSelection, "version">) =>
  `${sel.version}|${sel.screen}|${sel.rotation}|${sel.screens.join(",")}`;

export function fromSelection(sel: SelectionPart): ViewState {
  const screen = sel.screens.includes(sel.screen) || sel.screens.length === 0 ? sel.screen : sel.screens[0];
  return { screen, rotating: sel.rotation && sel.screens.length > 1, paused: false };
}

function step(screens: TvScreenKey[], screen: TvScreenKey, by: number): TvScreenKey {
  if (screens.length === 0) return screen;
  const i = screens.indexOf(screen);
  const n = screens.length;
  return screens[(((i < 0 ? 0 : i + by) % n) + n) % n];
}

const SCREEN_OF: Partial<Record<KeyAction, TvScreenKey>> = { health: "health", campaign: "campaign", flow: "flow" };

/** one key (or the rotation's tick) applied to the view: the new view and a note to show */
export function viewStep(
  state: ViewState,
  action: KeyAction | "tick",
  screens: TvScreenKey[],
): { state: ViewState; note: string | null } {
  const many = screens.length > 1;
  switch (action) {
    case "tick":
      if (!state.rotating || state.paused || !many) return { state, note: null };
      return { state: { ...state, screen: step(screens, state.screen, 1) }, note: null };
    case "next":
    case "prev":
      if (!many) return { state, note: NOTES.oneScreen };
      return { state: { ...state, screen: step(screens, state.screen, action === "next" ? 1 : -1) }, note: null };
    case "toggle-pause":
      if (!state.rotating) return { state, note: NOTES.noRotation };
      return { state: { ...state, paused: !state.paused }, note: state.paused ? NOTES.resumed : NOTES.paused };
    case "rotation":
      if (!many) return { state, note: NOTES.oneScreen };
      return {
        state: { ...state, rotating: !state.rotating, paused: false },
        note: state.rotating ? NOTES.rotationOff : NOTES.rotationOn,
      };
    case "health":
    case "campaign":
    case "flow": {
      const target = SCREEN_OF[action]!;
      if (!screens.includes(target)) return { state, note: target === "flow" ? NOTES.flowNotAllowed : null };
      return { state: { ...state, screen: target }, note: null };
    }
    default:
      return { state, note: null };
  }
}
