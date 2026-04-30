<?php

namespace App\Controllers;

use Psr\Http\Message\ServerRequestInterface as Request;
use Psr\Http\Message\ResponseInterface as Response;
use App\Services\NodeService;
use App\Services\QuotaService;

/**
 * Controller para APIs de Chat (OpenAI compatible)
 */
class ChatController
{
    private NodeService $nodeService;
    private QuotaService $quotaService;

    public function __construct(NodeService $nodeService, QuotaService $quotaService)
    {
        $this->nodeService = $nodeService;
        $this->quotaService = $quotaService;
    }

    /**
     * POST /v1/chat/completions
     * Compatível com API OpenAI
     */
    public function chatCompletions(Request $request, Response $response, array $args): Response
    {
        $body = json_decode($request->getBody()->getContents(), true);
        
        if (!$body) {
            return $this->jsonResponse($response, 400, ['error' => 'Invalid JSON body']);
        }

        // Extrair usuário do token (implementar no middleware)
        $userId = $request->getAttribute('user_id') ?? 'anonymous';
        $apiKey = $request->getAttribute('api_key') ?? '';

        // Resolver modelo
        $model = $this->resolveModel($body['model'] ?? 'qwen3.6-plus');

        // Verificar quota
        $quotaCheck = $this->quotaService->checkQuota($userId, $model);
        
        if (!$quotaCheck['allowed']) {
            return $this->jsonResponse($response, 429, [
                'error' => [
                    'message' => 'Quota exceeded',
                    'type' => 'quota_error',
                    'daily_remaining' => $quotaCheck['daily_remaining'],
                    'monthly_remaining' => $quotaCheck['monthly_remaining'],
                ]
            ]);
        }

        // Preparar payload para Node
        $nodePayload = [
            'model' => $model,
            'messages' => $body['messages'] ?? [],
            'stream' => $body['stream'] ?? false,
            'temperature' => $body['temperature'] ?? 0.7,
            'max_tokens' => $body['max_tokens'] ?? 2048,
            'tools' => $body['tools'] ?? null,
            'tool_choice' => $body['tool_choice'] ?? null,
            'user_id' => $userId,
            'api_key' => $apiKey,
        ];

        // Se for stream, usar generator
        if ($body['stream'] ?? false) {
            return $this->streamResponse($response, $nodePayload, $userId, $model);
        }

        // Requisição síncrona
        try {
            $result = $this->nodeService->request('/chat/completions', $nodePayload);
            
            // Calcular tokens usados
            $outputTokens = $result['usage']['completion_tokens'] ?? 0;
            $inputTokens = $result['usage']['prompt_tokens'] ?? 0;
            
            // Atualizar quota
            $this->quotaService->updateUsage($userId, $model, $outputTokens);
            $this->quotaService->logRequest($userId, $model, $inputTokens, $outputTokens, 'success');

            return $this->jsonResponse($response, 200, $result);
        } catch (\Exception $e) {
            $this->quotaService->logRequest($userId, $model, 0, 0, 'error: ' . $e->getMessage());
            
            return $this->jsonResponse($response, 500, [
                'error' => [
                    'message' => 'Failed to process request: ' . $e->getMessage(),
                    'type' => 'server_error',
                ]
            ]);
        }
    }

    /**
     * Stream SSE response
     */
    private function streamResponse(Response $response, array $payload, string $userId, string $model): Response
    {
        return $response->withHeader('Content-Type', 'text/event-stream')
                        ->withHeader('Cache-Control', 'no-cache')
                        ->withHeader('Connection', 'keep-alive')
                        ->withHeader('X-Accel-Buffering', 'no') // Nginx
                        ->withBody(\Psr\Http\Message\Stream\create_for_generator(function () use ($payload, $userId, $model) {
                            $totalOutputTokens = 0;
                            
                            foreach ($this->nodeService->chatStream($payload) as $chunk) {
                                echo $chunk;
                                flush();
                                
                                // Parse chunk para contar tokens (simplificado)
                                if (strpos($chunk, 'data: [DONE]') === false) {
                                    $data = json_decode(substr($chunk, 6), true);
                                    if (isset($data['usage']['completion_tokens'])) {
                                        $totalOutputTokens = $data['usage']['completion_tokens'];
                                    }
                                }
                            }
                            
                            // Atualizar quota após stream completar
                            if ($totalOutputTokens > 0) {
                                $this->quotaService->updateUsage($userId, $model, $totalOutputTokens);
                            }
                        }));
    }

    /**
     * Resolve aliases de modelos para modelo Qwen real
     */
    private function resolveModel(string $model): string
    {
        $mapping = [
            // OpenAI aliases
            'gpt-4o' => 'qwen3.6-plus',
            'gpt-4-turbo' => 'qwen3.6-plus',
            'gpt-3.5-turbo' => 'qwen3.6-plus',
            
            // Anthropic aliases
            'claude-3-5-sonnet' => 'qwen3.6-plus',
            'claude-3-opus' => 'qwen3.6-plus',
            
            // Gemini aliases
            'gemini-pro' => 'qwen3.6-plus',
            
            // Qwen nativo
            'qwen3.6-plus' => 'qwen3.6-plus',
            'qwen-max' => 'qwen3.6-plus',
            'qwen-plus' => 'qwen3.6-plus',
        ];

        return $mapping[$model] ?? 'qwen3.6-plus';
    }

    /**
     * Helper para resposta JSON
     */
    private function jsonResponse(Response $response, int $status, array $data): Response
    {
        $response->getBody()->write(json_encode($data, JSON_UNESCAPED_UNICODE));
        return $response->withStatus($status)->withHeader('Content-Type', 'application/json');
    }
}
