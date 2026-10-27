# Baixador de Vídeos (web)

Cole links (Instagram, TikTok, YouTube e outros), baixe tudo num `.zip` separado por pastas,
com conversão opcional para 1080x1920 em H.264.

- Linha `## Nome` cria uma pasta no .zip; linhas com `#` são ignoradas.
- Cada navegador vê só os próprios downloads; o histórico fica salvo no navegador.
- Os downloads entram numa fila única (um de cada vez) e ficam no servidor por 6 horas.

Roda com `python app.py` (precisa de `yt-dlp` e `ffmpeg` no PATH) ou pelo `Dockerfile`.
Variáveis opcionais: `PORT`, `DATA_DIR`, `KEEP_HOURS` (padrão 6), `MAX_LINKS` (padrão 300).

Para atualizar o yt-dlp, basta fazer um novo deploy (a imagem instala a versão mais recente).
