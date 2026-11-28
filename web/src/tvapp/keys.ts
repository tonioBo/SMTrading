/**
 * The remote of the Samsung TV (Tizen) for the «SMTrading TV» app. Arrows, OK and Back
 * reach a web app on their own; the colour keys and Play/Pause only once registered
 * (`tizen.tvinputdevice.registerKey`, privilege tv.inputdevice). In a desktop browser the
 * same actions answer to the arrows, Enter, Escape and R/G/Y/B (to try the app on a PC).
 */

export type KeyAction =
  | "prev"
  | "next"
  | "toggle-pause"
  | "health"
  | "campaign"
  | "flow"
  | "rotation"
  | "back";

/** Tizen key codes (TV remote) */
export const KEY = {
  LEFT: 37,
  RIGHT: 39,
  ENTER: 13,
  BACK: 10009,
  RED: 403,
  GREEN: 404,
  YELLOW: 405,
  BLUE: 406,
  PLAY_PAUSE: 10252,
} as const;

/** the keys to register with `tizen.tvinputdevice` (names of the Tizen TV key list) */
export const TIZEN_KEYS = ["ColorF0Red", "ColorF1Green", "ColorF2Yellow", "ColorF3Blue", "MediaPlayPause"] as const;

const BY_CODE: Record<number, KeyAction> = {
  [KEY.LEFT]: "prev",
  [KEY.RIGHT]: "next",
  [KEY.ENTER]: "toggle-pause",
  [KEY.PLAY_PAUSE]: "toggle-pause",
  [KEY.BACK]: "back",
  [KEY.RED]: "health",
  [KEY.GREEN]: "campaign",
  [KEY.YELLOW]: "flow",
  [KEY.BLUE]: "rotation",
};

const BY_NAME: Record<string, KeyAction> = {
  ArrowLeft: "prev",
  ArrowRight: "next",
  Enter: "toggle-pause",
  MediaPlayPause: "toggle-pause",
  XF86Back: "back",
  Escape: "back",
  ColorF0Red: "health",
  ColorF1Green: "campaign",
  ColorF2Yellow: "flow",
  ColorF3Blue: "rotation",
  r: "health",
  g: "campaign",
  y: "flow",
  b: "rotation",
};

/** what a key press means for the app, or null (a key it leaves alone) */
export function keyAction(e: { keyCode?: number; key?: string }): KeyAction | null {
  if (e.keyCode !== undefined && BY_CODE[e.keyCode]) return BY_CODE[e.keyCode];
  if (e.key && Object.prototype.hasOwnProperty.call(BY_NAME, e.key)) return BY_NAME[e.key];
  return null;
}

interface TizenGlobal {
  tvinputdevice?: { registerKey(name: string): void };
  application?: { getCurrentApplication(): { exit(): void } };
}

const tizen = (): TizenGlobal | undefined => (globalThis as { tizen?: TizenGlobal }).tizen;

/** register the colour keys and Play/Pause; the number registered (0 outside a TV) */
export function registerKeys(t: TizenGlobal | undefined = tizen()): number {
  let n = 0;
  for (const name of TIZEN_KEYS) {
    try {
      t?.tvinputdevice?.registerKey(name);
      if (t?.tvinputdevice) n++;
    } catch {
      // a key the model does not have: the others still work
    }
  }
  return n;
}

/** Back: close the app (on a TV); false outside a TV */
export function exitApp(t: TizenGlobal | undefined = tizen()): boolean {
  try {
    const app = t?.application?.getCurrentApplication();
    if (!app) return false;
    app.exit();
    return true;
  } catch {
    return false;
  }
}

/** the legend shown at start and on request */
export const KEYS_LEGEND = "◀ ▶ schermata · OK pausa · rosso Salute · verde Campagna · giallo Fiume · blu rotazione · Indietro esce";
