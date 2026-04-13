/**
 * 后端 API 基础地址。
 *
 * - 本地开发：留空，由 Vite proxy 代理到 http://localhost:7860
 * - Docker 生产：留空，由 nginx proxy 代理到 backend:7860
 * - Vercel / 独立前端：设置 VITE_API_BASE_URL=https://your-backend.example.com
 */
export const API_BASE: string = (import.meta.env.VITE_API_BASE_URL as string) ?? ''
