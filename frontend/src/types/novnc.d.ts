declare module '@novnc/novnc' {
  export interface RfbOptions {
    shared?: boolean;
    credentials?: { username?: string; password?: string; target?: string };
    repeaterID?: string;
    wsProtocols?: string[];
  }

  export interface RfbEventMap {
    connect: Event;
    disconnect: CustomEvent<{ clean: boolean }>;
    securityfailure: CustomEvent<{ status: number; reason: string }>;
    credentialsrequired: Event;
  }

  export default class RFB extends EventTarget {
    constructor(
      target: HTMLElement,
      urlOrChannel: string | WebSocket | RTCDataChannel,
      options?: RfbOptions,
    );
    viewOnly: boolean;
    clipViewport: boolean;
    dragViewport: boolean;
    scaleViewport: boolean;
    resizeSession: boolean;
    focusOnClick: boolean;
    showDotCursor: boolean;
    disconnect(): void;
    focus(options?: FocusOptions): void;
    blur(): void;
    sendKey(keysym: number, code: string | null, down?: boolean): void;
    clipboardPasteFrom(text: string): void;
  }
}
