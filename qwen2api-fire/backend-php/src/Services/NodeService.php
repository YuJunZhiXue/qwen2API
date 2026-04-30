<?php

namespace App\Services;

use GuzzleHttp\Client;
use GuzzleHttp\Exception\RequestException;

/**
 * Serviço de comunicação com o Node.js Service
 * Responsável por encaminhar requisições e receber streams
 */
class NodeService
{
    private Client $client;
    private string $nodeUrl;
    private string $apiKey;

    public function __construct()
    {
        $this->nodeUrl = getenv('NODE_SERVICE_URL') ?: 'http://localhost:3000';
        $this->apiKey = getenv('NODE_API_KEY') ?: 'secret-key-change-in-production';
        
        $this->client = new Client([
            'base_uri' => $this->nodeUrl,
            'timeout' => 120.0,
            'connect_timeout' => 10.0,
            'headers' => [
                'X-API-Key' => $this->apiKey,
                'Content-Type' => 'application/json',
            ],
        ]);
    }

    /**
     * Envia requisição de chat para o Node e retorna stream SSE
     */
    public function chatStream(array $payload): \Generator
    {
        try {
            $response = $this->client->post('/chat/completions', [
                'json' => $payload,
                'stream' => true,
            ]);

            $stream = $response->getBody();
            
            while (!$stream->eof()) {
                $line = $stream->fgets(4096);
                if ($line === false) {
                    break;
                }
                
                // Repassa chunk SSE diretamente para o cliente
                yield $line;
            }
        } catch (RequestException $e) {
            $statusCode = $e->getResponse()?->getStatusCode() ?? 500;
            $errorBody = $e->getResponse()?->getBody()?->getContents() ?? 'Unknown error';
            
            yield "data: " . json_encode([
                'error' => [
                    'message' => 'Node service error: ' . $e->getMessage(),
                    'type' => 'node_error',
                    'status' => $statusCode,
                    'details' => $errorBody,
                ]
            ]) . "\n\n";
        }
    }

    /**
     * Requisição síncrona para operações sem stream
     */
    public function request(string $endpoint, array $data): array
    {
        try {
            $response = $this->client->post($endpoint, [
                'json' => $data,
            ]);

            return json_decode($response->getBody()->getContents(), true);
        } catch (RequestException $e) {
            throw new \Exception(
                'Node service error: ' . $e->getMessage(),
                $e->getResponse()?->getStatusCode() ?? 500
            );
        }
    }

    /**
     * Verifica saúde do serviço Node
     */
    public function healthCheck(): bool
    {
        try {
            $response = $this->client->get('/health');
            return $response->getStatusCode() === 200;
        } catch (\Exception $e) {
            return false;
        }
    }

    /**
     * Gerencia conta Qwen (login, refresh token)
     */
    public function manageAccount(string $action, array $data): array
    {
        return $this->request('/accounts/manage', [
            'action' => $action,
            'data' => $data,
        ]);
    }
}
