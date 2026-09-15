import { Injectable } from '@angular/core';
import RFB from '@novnc/novnc';

export type RfbConnectionEvent =
  | Event
  | CustomEvent<{ clean: boolean }>
  | CustomEvent<{
      status: number;
      reason: string;
    }>;

export interface RfbLike {
  viewOnly: boolean;
  clipViewport: boolean;
  dragViewport: boolean;
  scaleViewport: boolean;
  resizeSession: boolean;
  focusOnClick: boolean;
  addEventListener(type: string, listener: (event: RfbConnectionEvent) => void): void;
  removeEventListener(type: string, listener: (event: RfbConnectionEvent) => void): void;
  disconnect(): void;
  focus(options?: FocusOptions): void;
  sendKey(keysym: number, code: string | null, down?: boolean): void;
  clipboardPasteFrom(text: string): void;
}

export type RfbFactory = (
  target: HTMLElement,
  url: string,
  credentials: { password: string },
) => RfbLike;

@Injectable({ providedIn: 'root' })
export class VncClientService {
  readonly create: RfbFactory = (target, url, credentials) =>
    // SAFETY: @novnc/novnc exposes the runtime RFB API without declaration metadata.
    new RFB(target, url, { shared: true, credentials }) as unknown as RfbLike;
}
