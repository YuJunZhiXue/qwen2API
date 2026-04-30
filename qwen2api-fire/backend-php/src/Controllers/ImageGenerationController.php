<?php

namespace App\Controllers;

use Psr\Http\Message\ResponseInterface as Response;
use Psr\Http\Message\ServerRequestInterface as Request;
use Slim\Psr7\Factory\ResponseFactory;

/**
 * Image Generation Controller
 * Detecta prompts de geração de imagens e roteia para serviço Node.js
 */
class ImageGenerationController
{
    private \PDO $db;
    private string $nodeServiceUrl;

    // Keywords que indicam geração de imagem
    private array $imageKeywords = [
        'draw', 'drawing', 'generate image', 'create image', 'make image',
        'gerar imagem', 'criar imagem', 'desenhar', 'ilustrar',
        '生成图片', '画图', '绘制', '图像生成',
        '画像を生成', '描画', 'イメージを作成',
        'immagine', 'disegna', 'genera immagine',
        'bild erstellen', 'zeichnen', 'bild generieren'
    ];

    public function __construct(\PDO $db, string $nodeServiceUrl)
    {
        $this->db = $db;
        $this->nodeServiceUrl = rtrim($nodeServiceUrl, '/');
    }

    /**
     * Detecta se prompt é para geração de imagem
     */
    public function isImageRequest(string $prompt): bool
    {
        $promptLower = strtolower($prompt);
        
        foreach ($this->imageKeywords as $keyword) {
            if (strpos($promptLower, strtolower($keyword)) !== false) {
                return true;
            }
        }

        // Detecta padrões como "imagem de...", "foto de...", etc
        $patterns = [
            '/^(crie|gere|faça|cria|gera|faz)\s+uma?\s*(imagem|foto|ilustra[çc][aã]o|desenho)/i',
            '/^(crie|gere|faça|cria|gera|faz)\s+(algo|uma?\s*cena|um\s+quadro)/i',
            '/^desenha\s+/i',
            '/^mostre\s+(uma?\s*)?(imagem|foto)/i'
        ];

        foreach ($patterns as $pattern) {
            if (preg_match($pattern, $prompt)) {
                return true;
            }
        }

        return false;
    }

    /**
     * Processa requisição de geração de imagem
     */
    public function generateImage(Request $request): Response
    {
        $responseFactory = new ResponseFactory();
        $response = $responseFactory->createResponse();
        
        try {
            $body = json_decode($request->getBody()->getContents(), true);
            
            if (!isset($body['prompt']) && !isset($body['messages'])) {
                return $response->withStatus(400)
                    ->withHeader('Content-Type', 'application/json')
                    ->withBody(\Slim\Psr7\Stream::create(json_encode([
                        'error' => ['message' => 'Prompt or messages required']
                    ])));
            }

            $prompt = $body['prompt'] ?? '';
            if (empty($prompt) && isset($body['messages'])) {
                // Extrai último message do usuário
                $messages = $body['messages'];
                for ($i = count($messages) - 1; $i >= 0; $i--) {
                    if ($messages[$i]['role'] === 'user') {
                        $prompt = $messages[$i]['content'];
                        break;
                    }
                }
            }

            if (empty($prompt)) {
                return $response->withStatus(400)
                    ->withHeader('Content-Type', 'application/json')
                    ->withBody(\Slim\Psr7\Stream::create(json_encode([
                        'error' => ['message' => 'No prompt found in messages']
                    ])));
            }

            // Verifica se realmente é request de imagem
            if (!$this->isImageRequest($prompt)) {
                return $response->withStatus(400)
                    ->withHeader('Content-Type', 'application/json')
                    ->withBody(\Slim\Psr7\Stream::create(json_encode([
                        'error' => ['message' => 'Not an image generation request']
                    ])));
            }

            // Chama serviço Node.js para gerar imagem
            $imageUrl = $this->callNodeImageService($prompt, $body);

            // Retorna resposta no formato OpenAI
            $responseData = [
                'id' => 'img_' . time() . '_' . bin2hex(random_bytes(8)),
                'object' => 'text_completion',
                'created' => time(),
                'model' => 'qwen-vl-max',
                'choices' => [
                    [
                        'index' => 0,
                        'message' => [
                            'role' => 'assistant',
                            'content' => "Aqui está sua imagem:\n\n![Generated Image](" . $imageUrl . ")",
                            'tool_calls' => []
                        ],
                        'finish_reason' => 'stop'
                    ]
                ],
                'usage' => [
                    'prompt_tokens' => str_word_count($prompt),
                    'completion_tokens' => 50,
                    'total_tokens' => str_word_count($prompt) + 50
                ]
            ];

            return $response->withHeader('Content-Type', 'application/json')
                ->withBody(\Slim\Psr7\Stream::create(json_encode($responseData)));

        } catch (\Exception $e) {
            error_log('[ImageGen] Error: ' . $e->getMessage());
            
            return $response->withStatus(500)
                ->withHeader('Content-Type', 'application/json')
                ->withBody(\Slim\Psr7\Stream::create(json_encode([
                    'error' => [
                        'message' => 'Failed to generate image: ' . $e->getMessage(),
                        'type' => 'image_generation_error'
                    ]
                ])));
        }
    }

    /**
     * Chama serviço Node.js para geração de imagem
     */
    private function callNodeImageService(string $prompt, array $options): string
    {
        $ch = curl_init($this->nodeServiceUrl . '/api/generate-image');
        
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_POST => true,
            CURLOPT_HTTPHEADER => [
                'Content-Type: application/json',
                'X-API-Key: ' . ($_ENV['NODE_SERVICE_KEY'] ?? 'internal-key')
            ],
            CURLOPT_POSTFIELDS => json_encode([
                'prompt' => $prompt,
                'size' => $options['size'] ?? '1024x1024',
                'quality' => $options['quality'] ?? 'standard',
                'style' => $options['style'] ?? 'natural'
            ]),
            CURLOPT_TIMEOUT => 60 // Imagens podem demorar
        ]);

        $result = curl_exec($ch);
        $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $error = curl_error($ch);
        
        curl_close($ch);

        if ($error || $httpCode !== 200) {
            throw new \Exception('Node service error: ' . ($error ?: 'HTTP ' . $httpCode));
        }

        $data = json_decode($result, true);
        
        if (!isset($data['url'])) {
            throw new \Exception('No image URL returned from service');
        }

        return $data['url'];
    }

    /**
     * Endpoint para upload de imagem (opcional)
     */
    public function uploadImage(Request $request): Response
    {
        $responseFactory = new ResponseFactory();
        $response = $responseFactory->createResponse();

        try {
            $uploadedFiles = $request->getUploadedFiles();
            
            if (empty($uploadedFiles['image'])) {
                return $response->withStatus(400)
                    ->withHeader('Content-Type', 'application/json')
                    ->withBody(\Slim\Psr7\Stream::create(json_encode([
                        'error' => ['message' => 'No image file uploaded']
                    ])));
            }

            $file = $uploadedFiles['image'];
            
            // Valida tipo
            $allowedTypes = ['image/jpeg', 'image/png', 'image/gif', 'image/webp'];
            if (!in_array($file->getClientMediaType(), $allowedTypes)) {
                return $response->withStatus(400)
                    ->withHeader('Content-Type', 'application/json')
                    ->withBody(\Slim\Psr7\Stream::create(json_encode([
                        'error' => ['message' => 'Invalid file type. Allowed: jpg, png, gif, webp']
                    ])));
            }

            // Valida tamanho (max 10MB)
            if ($file->getSize() > 10 * 1024 * 1024) {
                return $response->withStatus(400)
                    ->withHeader('Content-Type', 'application/json')
                    ->withBody(\Slim\Psr7\Stream::create(json_encode([
                        'error' => ['message' => 'File too large. Max 10MB']
                    ])));
            }

            // Gera nome único
            $filename = uniqid('img_') . '.' . pathinfo($file->getClientFilename(), PATHINFO_EXTENSION);
            $uploadPath = __DIR__ . '/../../uploads/' . $filename;

            // Move arquivo
            $file->moveTo($uploadPath);

            // Retorna URL
            $imageUrl = '/uploads/' . $filename;

            return $response->withHeader('Content-Type', 'application/json')
                ->withBody(\Slim\Psr7\Stream::create(json_encode([
                    'url' => $imageUrl,
                    'filename' => $filename,
                    'size' => $file->getSize()
                ])));

        } catch (\Exception $e) {
            return $response->withStatus(500)
                ->withHeader('Content-Type', 'application/json')
                ->withBody(\Slim\Psr7\Stream::create(json_encode([
                    'error' => ['message' => 'Upload failed: ' . $e->getMessage()]
                ])));
        }
    }
}
