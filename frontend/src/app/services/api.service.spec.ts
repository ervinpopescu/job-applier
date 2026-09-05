import { describe, it, expect, beforeEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ApiService, resolveApiUrl, resolveFileUrl } from './api.service';

describe('ApiService - URL Resolution', () => {
  describe('resolveApiUrl', () => {
    it('resolves relative API path against root baseURI', () => {
      const resolved = resolveApiUrl('/api/stats', 'http://127.0.0.1:8000/');
      expect(resolved).toBe('/api/stats');
    });

    it('resolves relative API path against subpath baseURI', () => {
      const resolved = resolveApiUrl('/api/stats', 'https://host.example/job-applier/');
      expect(resolved).toBe('/job-applier/api/stats');
    });

    it('handles baseURI without trailing slash gracefully', () => {
      const resolved = resolveApiUrl('/api/stats', 'https://host.example/job-applier');
      expect(resolved).toBe('/job-applier/api/stats');
    });

    it('does not double-prefix already prefixed paths', () => {
      const resolved = resolveApiUrl(
        '/job-applier/api/applications',
        'https://host.example/job-applier/',
      );
      expect(resolved).toBe('/job-applier/api/applications');
    });

    it('does not double-prefix file storage URLs', () => {
      const resolved = resolveApiUrl(
        '/job-applier/files/applications/sample/CV.pdf',
        'https://host.example/job-applier/',
      );
      expect(resolved).toBe('/job-applier/files/applications/sample/CV.pdf');
    });

    it('prefixes unprefixed file paths under subpath', () => {
      const resolved = resolveApiUrl(
        '/files/applications/sample/CV.pdf',
        'https://host.example/job-applier/',
      );
      expect(resolved).toBe('/job-applier/files/applications/sample/CV.pdf');
    });

    it('preserves absolute external URLs with scheme', () => {
      expect(resolveApiUrl('https://example.com/api/test')).toBe('https://example.com/api/test');
      expect(resolveApiUrl('http://other.org/files/cv.pdf')).toBe('http://other.org/files/cv.pdf');
      expect(resolveApiUrl('blob:http://localhost:8000/123')).toBe(
        'blob:http://localhost:8000/123',
      );
      expect(resolveApiUrl('data:image/svg+xml;base64,...')).toBe('data:image/svg+xml;base64,...');
    });

    it('handles query strings correctly', () => {
      const resolved = resolveApiUrl(
        '/api/applications?limit=20&offset=10',
        'https://host.example/job-applier/',
      );
      expect(resolved).toBe('/job-applier/api/applications?limit=20&offset=10');
    });

    it('returns empty string for empty input', () => {
      expect(resolveApiUrl('')).toBe('');
    });
  });

  describe('resolveFileUrl', () => {
    it('returns empty string for null or undefined', () => {
      expect(resolveFileUrl(null)).toBe('');
      expect(resolveFileUrl(undefined)).toBe('');
    });

    it('resolves valid file path against baseURI', () => {
      expect(resolveFileUrl('/files/test.pdf', '/sub/')).toBe('/sub/files/test.pdf');
    });
  });
});

describe('ApiService - HTTP Requests', () => {
  let service: ApiService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [ApiService, provideHttpClient(), provideHttpClientTesting()],
    });

    service = TestBed.inject(ApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  it('getStats calls resolved /api/stats endpoint', () => {
    service.getStats().subscribe();
    const req = httpMock.expectOne((r) => r.url.endsWith('/api/stats'));
    expect(req.request.method).toBe('GET');
    req.flush({ pending: 5, applied_folders: 2 });
    httpMock.verify();
  });

  it('getExportUrl returns resolved export link', () => {
    const url = service.getExportUrl();
    expect(url.endsWith('/api/export')).toBe(true);
  });

  it('importBackup posts FormData to resolved /api/import endpoint', () => {
    const fd = new FormData();
    service.importBackup(fd).subscribe();
    const req = httpMock.expectOne((r) => r.url.endsWith('/api/import'));
    expect(req.request.method).toBe('POST');
    req.flush({ status: 'success', imported_applications: 3 });
    httpMock.verify();
  });
});
