# Auvello — vídeos no seu WhatsApp

## Já configurado

Destino: **5528999676818**. Se essa for a conta conectada ao Baileys, o material
vai para a conversa da própria conta (Mensagem para você).

Após a descoberta de ofertas, o projeto escolhe os melhores candidatos por score
e gera até três vídeos por dia. Você recebe:

1. Um **arquivo MP4**, enviado como documento para preservar o arquivo original.
2. Uma mensagem separada com a descrição completa, hashtags e link de afiliado,
   pronta para copiar e publicar no TikTok.

Formato: 1080 × 1920, 15 segundos, imagens e textos com transições. Sem música
ou narração; você pode adicionar áudio no TikTok. Nenhuma API paga é usada.
Os materiais não são enviados aos grupos; o envio de ofertas aos grupos continua.

## Atualizar no Render

1. Faça uma cópia do projeto atual antes da atualização.
2. Envie o conteúdo desta pasta ao mesmo repositório conectado ao Render.
3. **Mantenha todas as variáveis existentes no Render**, especialmente DATABASE_URL,
   credenciais das lojas e IDs dos grupos. Não substitua seu `.env` pelo exemplo.
4. Faça o deploy usando o Dockerfile deste projeto e o `start.sh` incluído.
5. Se a sessão de WhatsApp já está no Neon, ela será reutilizada. Caso o painel
   indique que desconectou, reconecte pelo QR como você já faz.

O Dockerfile instala FFmpeg e fontes. O start.sh gera automaticamente uma chave
interna compartilhada entre Python e Node quando WHATSAPP_API_SECRET estiver
vazia. Se você já usa essa variável, o valor existente será preservado.
**Não é preciso contratar outro serviço ou configurar uma API do TikTok.**

Valores padrão funcionam mesmo sem adicionar novas variáveis:

```env
VIDEOS_ENABLED=true
VIDEOS_WHATSAPP_PHONE=5528999676818
VIDEOS_MAX_PER_DAY=3
VIDEOS_DURATION_SECONDS=15
```

VIDEOS_ENABLED=false desativa geração e entrega dos vídeos. Limite zero interrompe
novas gerações, mas ainda permite entregar os materiais já pendentes.

## Banco e reinícios

Com DATABASE_URL configurada, são criadas automaticamente duas tabelas novas:
`auvello_video_jobs` (fila e arquivos pendentes) e `auvello_video_deliveries`
(controle das mensagens enviadas). As tabelas existentes não são substituídas.

O MP4 e a descrição ficam no banco antes de qualquer envio. Assim, uma queda do
Render não perde o material pendente. O envio é retomado nas próximas rodadas.
Após enviar o vídeo e a descrição, o MP4 é removido do banco e permanece o controle
para não repetir o produto. Se apenas a descrição falhar, só ela é retomada.
Materiais não enviados em 48 horas expiram para não divulgar preços muito antigos.
Os arquivos usados na renderização são temporários e removidos depois.

Sem DATABASE_URL, a fila usa o SQLite local do projeto. No Render, mantenha a
conexão com o Neon para persistência após reiniciar. Python e Node precisam usar
a mesma DATABASE_URL e a mesma VIDEOS_WHATSAPP_PHONE.

O serviço registra confirmações por etapa e reutiliza o mesmo identificador de
mensagem em tentativas de rede. Falhas ocorridas no exato intervalo entre o envio
no WhatsApp e a confirmação no banco não permitem uma garantia absoluta de
entrega única; os controles reduzem repetições nessa situação.

## Comandos

- `python main.py`: rotina normal, incluindo vídeos e entrega ao número pessoal.
- `python main.py --once`: uma rodada normal.
- `python main.py --videos`: usa as ofertas já no cache e entrega os vídeos ao
  número pessoal; não faz nova descoberta nem envia ofertas aos grupos.
- `python main.py --video-demo`: gera exemplo fictício offline em videos_demo;
  **não envia nada ao WhatsApp**.

Se executar Python e Node separadamente, configure o mesmo WHATSAPP_API_SECRET
nos dois processos. No Docker/start.sh isso é automático.

## Publicação

Baixe o MP4 na sua conversa do WhatsApp, envie ao TikTok e copie a descrição.
Confira preço, estoque e frete antes de publicar. A legenda inclui a informação
de link de afiliado; faça também a identificação comercial apropriada no TikTok.
Um link escrito na legenda não garante um link clicável.

## Validação

Foram verificados: geração real de um MP4 H.264 1080 × 1920 de 15 segundos;
limite diário; controle de produto após reiniciar; persistência da fila SQLite;
retomada da descrição sem reenviar o vídeo; contrato HTTP; resolução da própria
conta; validação de destino e mídia; bloqueio de acesso sem autorização;
controle de envio repetido e uso do mesmo ID em novas tentativas.

Testes usam banco temporário e conexão WhatsApp simulada. Não foi realizado envio
real pela sua sessão, deploy no seu Render ou conexão ao seu banco Neon. Essas
etapas exigem seu ambiente existente. Confira nos logs `[videos]` após o deploy.

```bash
python -m unittest test_videos test_video_jobs -v
node --test whatsapp-service/test-video-delivery.js
```

A geração usa CPU, disco temporário e banco do ambiente que você já utiliza;
o tempo da rodada pode aumentar em máquinas pequenas. O limite de três vídeos
por dia mantém o processamento e armazenamento pendente limitados. O exemplo
`exemplo_video` é fictício e serve apenas para conferir o formato.
