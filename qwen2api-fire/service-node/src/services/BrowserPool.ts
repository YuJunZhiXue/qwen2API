import { chromium, Browser, BrowserContext, Page } from 'playwright';
import * as crypto from 'crypto';
import pino from 'pino';
import { BrowserSession, AccountCredentials } from '../types';

const logger = pino({ level: process.env.LOG_LEVEL || 'info' });

export class BrowserPool {
  private sessions: Map<string, BrowserSession> = new Map();
  private poolSize: number;
  private maxInflight: number;
  private browserTimeout: number;

  constructor() {
    this.poolSize = parseInt(process.env.BROWSER_POOL_SIZE || '2');
    this.maxInflight = parseInt(process.env.MAX_INFLIGHT_PER_BROWSER || '1');
    this.browserTimeout = parseInt(process.env.BROWSER_TIMEOUT_MS || '300000');
  }

  /**
   * Obtém ou cria uma sessão do pool
   */
  async acquireSession(): Promise<BrowserSession> {
    // Encontrar sessão disponível
    for (const [id, session] of this.sessions) {
      const age = Date.now() - session.createdAt;
      const idleTime = Date.now() - session.lastUsed;
      
      if (session.requestCount < this.maxInflight && 
          age < this.browserTimeout &&
          !this.isSessionStale(session)) {
        
        session.lastUsed = Date.now();
        session.requestCount++;
        logger.debug({ sessionId: id }, 'Reusing browser session');
        return session;
      }
    }

    // Criar nova sessão se pool não estiver cheio
    if (this.sessions.size < this.poolSize) {
      return this.createSession();
    }

    // Aguardar sessão disponível
    logger.warn('Browser pool full, waiting...');
    await this.sleep(1000);
    return this.acquireSession();
  }

  /**
   * Cria nova sessão de navegador
   */
  private async createSession(): Promise<BrowserSession> {
    const sessionId = crypto.randomUUID();
    logger.info({ sessionId }, 'Creating new browser session');

    const browser = await chromium.launch({
      headless: true,
      args: [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
        '--disable-accelerated-2d-canvas',
        '--disable-gpu',
      ],
    });

    const context = await browser.newContext({
      viewport: { width: 1920, height: 1080 },
      userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
      locale: 'en-US',
      timezoneId: 'America/New_York',
    });

    const page = await context.newPage();

    const session: BrowserSession = {
      id: sessionId,
      browser,
      context,
      page,
      createdAt: Date.now(),
      lastUsed: Date.now(),
      requestCount: 1,
      isLoggedIn: false,
    };

    this.sessions.set(sessionId, session);
    logger.info({ sessionId, poolSize: this.sessions.size }, 'Browser session created');

    return session;
  }

  /**
   * Libera sessão de volta ao pool
   */
  releaseSession(sessionId: string): void {
    const session = this.sessions.get(sessionId);
    if (session) {
      session.requestCount = Math.max(0, session.requestCount - 1);
      session.lastUsed = Date.now();
      logger.debug({ sessionId }, 'Session released');
    }
  }

  /**
   * Remove sessão do pool e fecha navegador
   */
  async removeSession(sessionId: string): Promise<void> {
    const session = this.sessions.get(sessionId);
    if (session) {
      try {
        await session.browser.close();
      } catch (error) {
        logger.error({ sessionId, error }, 'Error closing browser');
      }
      this.sessions.delete(sessionId);
      logger.info({ sessionId }, 'Session removed');
    }
  }

  /**
   * Verifica se sessão está obsoleta
   */
  private isSessionStale(session: BrowserSession): boolean {
    // Implementar verificação de health do browser
    return false;
  }

  /**
   * Limpa sessões expiradas
   */
  async cleanup(): Promise<void> {
    const now = Date.now();
    for (const [id, session] of this.sessions) {
      if (now - session.createdAt > this.browserTimeout) {
        await this.removeSession(id);
      }
    }
  }

  /**
   * Fecha todas as sessões
   */
  async shutdown(): Promise<void> {
    logger.info('Shutting down browser pool');
    for (const sessionId of this.sessions.keys()) {
      await this.removeSession(sessionId);
    }
  }

  private sleep(ms: number): Promise<void> {
    return new Promise(resolve => setTimeout(resolve, ms));
  }
}
