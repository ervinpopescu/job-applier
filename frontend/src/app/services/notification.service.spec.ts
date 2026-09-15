import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { NotificationService } from './notification.service';
import { ApiService } from './api.service';

describe('NotificationService', () => {
  let service: NotificationService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    localStorage.clear();
    TestBed.configureTestingModule({
      providers: [NotificationService, ApiService, provideHttpClient(), provideHttpClientTesting()],
    });

    service = TestBed.inject(NotificationService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  it('initializes with default preferences (sound OFF, native notifications disabled)', () => {
    expect(service.soundEnabled()).toBe(false);
    expect(service.nativeEnabled()).toBe(false);
    expect(service.unreadCount()).toBe(0);
    expect(service.notifications()).toEqual([]);
  });

  it('updates and persists sound preference when toggled', () => {
    service.setSoundEnabled(true);
    expect(service.soundEnabled()).toBe(true);
    expect(localStorage.getItem('job_applier_notification_sound_enabled')).toBe('true');

    service.setSoundEnabled(false);
    expect(service.soundEnabled()).toBe(false);
    expect(localStorage.getItem('job_applier_notification_sound_enabled')).toBe('false');
  });

  it('detects secure origin correctly', () => {
    expect(service.isSecureOrigin()).toBe(true);
  });

  it('fetches notifications and updates unread count', () => {
    service.fetchNotifications();

    const req = httpMock.expectOne('/api/notifications?limit=50&unread_only=false');
    expect(req.request.method).toBe('GET');

    req.flush({
      status: 'success',
      notifications: [
        {
          id: 1,
          notification_id: 'notif_abc',
          category: 'captcha_required',
          severity: 'critical',
          title: 'Action Required: CAPTCHA',
          message: 'Please complete CAPTCHA in browser.',
          acknowledged: false,
          created_at: '2026-09-05 21:00:00',
        },
      ],
      unread_count: 1,
      total: 1,
      latest_id: 1,
    });

    expect(service.notifications().length).toBe(1);
    expect(service.unreadCount()).toBe(1);
    expect(service.latestNotificationId()).toBe(1);
  });

  it('acknowledges a notification idempotently without modifying paused state', () => {
    // Set initial state
    service.notifications.set([
      {
        id: 10,
        notification_id: 'notif_10',
        category: 'mfa_required',
        severity: 'error',
        title: 'Action Required: MFA',
        message: 'Enter code.',
        acknowledged: false,
        created_at: '2026-09-05 21:00:00',
      },
    ]);
    service.unreadCount.set(1);
    service.isPaused.set(true);

    service.ackNotification(10);

    const req = httpMock.expectOne('/api/notifications/10/ack');
    expect(req.request.method).toBe('POST');
    req.flush({
      status: 'success',
      id: 10,
      notification_id: 'notif_10',
      acknowledged: true,
      already_acknowledged: false,
    });

    expect(service.notifications()[0].acknowledged).toBe(true);
    expect(service.unreadCount()).toBe(0);
    // Pause state is strictly preserved (acknowledging does not resume)
    expect(service.isPaused()).toBe(true);
  });

  it('acknowledges all notifications via ackAll()', () => {
    service.notifications.set([
      {
        id: 1,
        notification_id: 'notif_1',
        category: 'c1',
        severity: 'info',
        title: 'T1',
        message: 'M1',
        acknowledged: false,
        created_at: '2026-09-05',
      },
      {
        id: 2,
        notification_id: 'notif_2',
        category: 'c2',
        severity: 'warning',
        title: 'T2',
        message: 'M2',
        acknowledged: false,
        created_at: '2026-09-05',
      },
    ]);
    service.unreadCount.set(2);

    service.ackAll();

    const req = httpMock.expectOne('/api/notifications/ack-all');
    expect(req.request.method).toBe('POST');
    req.flush({
      status: 'success',
      acknowledged_count: 2,
      message: 'Acknowledged 2 notifications.',
    });

    expect(service.unreadCount()).toBe(0);
    expect(service.notifications().every((n) => n.acknowledged)).toBe(true);
  });

  it('plays synthesized sound safely when sound is enabled', () => {
    service.soundEnabled.set(true);
    // Mock AudioContext
    const mockOsc = {
      type: '',
      frequency: { setValueAtTime: vi.fn() },
      connect: vi.fn(),
      start: vi.fn(),
      stop: vi.fn(),
    };
    const mockGain = {
      gain: { setValueAtTime: vi.fn(), exponentialRampToValueAtTime: vi.fn() },
      connect: vi.fn(),
    };
    const mockCtx = {
      currentTime: 0,
      state: 'running',
      destination: {},
      createOscillator: vi.fn().mockReturnValue(mockOsc),
      createGain: vi.fn().mockReturnValue(mockGain),
    };

    (service as any).audioCtx = mockCtx;
    service.playNotificationSound();

    expect(mockCtx.createOscillator).toHaveBeenCalledTimes(2);
    expect(mockCtx.createGain).toHaveBeenCalledTimes(2);
  });

  it('sanitizes native OS notification previews to privacy-safe text without employer names or raw errors', () => {
    service.nativeEnabled.set(true);
    service.permissionState.set('granted');

    const notificationConstructorSpy = vi.fn();
    (globalThis as any).Notification = notificationConstructorSpy;

    const notif = {
      id: 99,
      notification_id: 'notif_99',
      category: 'captcha_required',
      severity: 'critical' as const,
      title: 'Action Required: Acme Corp',
      message: 'Application to Acme Corp halted: CaptchaDetectedError',
      acknowledged: false,
      created_at: '2026-09-05',
    };

    (service as any).dispatchDeduplicatedAlert(notif, 'tag-99');

    expect(notificationConstructorSpy).toHaveBeenCalled();
    const [title, options] = notificationConstructorSpy.mock.calls[0];
    expect(title).toBe('JobApplier: Captcha Required');
    expect(title).not.toContain('Acme Corp');
    expect(options.body).not.toContain('Acme Corp');
    expect(options.body).not.toContain('CaptchaDetectedError');
  });

  it('deduplicates alerts across tabs using localStorage timestamp', () => {
    service.nativeEnabled.set(true);
    service.permissionState.set('granted');

    const notificationConstructorSpy = vi.fn();
    (globalThis as any).Notification = notificationConstructorSpy;

    const notif = {
      id: 101,
      notification_id: 'notif_101',
      category: 'captcha_required',
      severity: 'critical' as const,
      title: 'Action Required',
      message: 'Captcha required',
      acknowledged: false,
      created_at: '2026-09-05',
    };

    (service as any).dispatchDeduplicatedAlert(notif, 'tag-101');
    expect(notificationConstructorSpy).toHaveBeenCalledTimes(1);

    // Subsequent dispatch with same tag within 5 seconds is suppressed
    (service as any).dispatchDeduplicatedAlert(notif, 'tag-101');
    expect(notificationConstructorSpy).toHaveBeenCalledTimes(1);
  });

  it('deduplicates alerts across tabs when Web Locks API is available', async () => {
    service.nativeEnabled.set(true);
    service.permissionState.set('granted');

    const notificationConstructorSpy = vi.fn();
    (globalThis as any).Notification = notificationConstructorSpy;

    const mockLocks = {
      request: vi.fn(async (name: string, options: any, callback: (lock: any) => Promise<any>) => {
        return callback({ name });
      }),
    };
    Object.defineProperty(navigator, 'locks', {
      value: mockLocks,
      configurable: true,
      writable: true,
    });

    const notif = {
      id: 102,
      notification_id: 'notif_102',
      category: 'mfa_required',
      severity: 'critical' as const,
      title: 'Action Required',
      message: 'MFA required',
      acknowledged: false,
      created_at: '2026-09-05',
    };

    (service as any).dispatchDeduplicatedAlert(notif, 'tag-102');
    expect(mockLocks.request).toHaveBeenCalledTimes(1);
    expect(notificationConstructorSpy).toHaveBeenCalledTimes(1);

    // Second call with same tag within 5 seconds is suppressed even with Web Locks
    (service as any).dispatchDeduplicatedAlert(notif, 'tag-102');
    expect(notificationConstructorSpy).toHaveBeenCalledTimes(1);
  });
});
