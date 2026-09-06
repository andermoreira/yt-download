# yt-down

Baixa vídeos de perfis do Instagram. Na segunda execução, só puxa o que ainda não está no disco nem no `data/archive.txt`.

Por padrão **não usa yt-dlp**: a listagem já traz `video_versions` (MP4 no CDN do Instagram), o mesmo campo que o [gallery-dl](https://github.com/mikf/gallery-dl) usa. O download é HTTP com a biblioteca padrão.

yt-dlp continua opcional (`--downloader yt-dlp`) e também serve só para exportar cookies do browser.

## Setup

Python 3.10+ (no macOS o `python3` do Xcode pode ser 3.9 — use o do Homebrew).

Não precisa instalar nada além do Python, se você já tiver um `cookies.txt` Netscape.

O Instagram exige sessão logada. Duas formas:

1. Exporte `cookies.txt` (extensão tipo *Get cookies.txt LOCALLY*) e coloque na raiz do projeto.
2. Ou, se o yt-dlp estiver instalado (Chrome fechado; perfil Default = `chrome`, outro = `chrome:Profile 1`):

```bash
python3 download_instagram.py --cookies-from-browser chrome
```

O dump **não** acessa o Instagram (o extractor do yt-dlp está quebrado e essa request pode invalidar a sessão). O User-Agent padrão é Chrome 152 — se o browser for outra major, passe `--user-agent` ou `IG_USER_AGENT`.

## Uso

Edite `profiles.txt` — um item por linha:

```
andres.ague
https://www.instagram.com/outro.perfil/
https://www.instagram.com/andres.ague/reel/CSIeW8lg-Pd/
```

```bash
python download_instagram.py
```

Arquivos ficam assim:

```
downloads/
  andres.ague/
    reels/
      2021-07-15_CSIeW8lg-Pd.mp4
    feed/
      2021-08-02_AbCdEfGhIjK.mp4
```

Um diretório por perfil, reels e posts do feed separados, data na frente do arquivo para o Finder ordenar. O shortcode no nome é o ID estável (serve para pular o que já baixou). Carousel vira `data_ID~1.mp4`, `data_ID~2.mp4`. Sem data na API: `undated_ID.mp4`.

IDs já baixados também ficam em `data/archive.txt`.

## Só os novos

O script considera um vídeo como já existente se:

1. o ID está em `data/archive.txt`; ou
2. já existe um arquivo com esse ID em `downloads/` (`2021-07-15_CSIeW8lg-Pd.mp4` ou o shortcode sozinho).

Em perfil, reels e feed são varridos **separados**. Depois de 3 vídeos seguidos que você já tem **naquela aba**, a varredura da aba para — pinned posts não entram nessa conta. Para varrer tudo:

```bash
python download_instagram.py --full
```

Na primeira sincronização de um perfil grande, limite o lote:

```bash
python download_instagram.py --max 20
```

Ao terminar, o script imprime `downloaded / skipped / failed` e sai com código `1` se algum download falhou. 429 e erros de rede tentam de novo (padrão: 3 retries). Linhas inválidas em `profiles.txt` são puladas com warning — não abortam a execução.

## Resume de varredura profunda

`--full` salva a posição da paginação em `data/cursors.json` depois de cada página consumida. Se a varredura for interrompida (Ctrl+C sai com código 130, `--max`, crash), a próxima `--full` do mesmo perfil retoma do ponto salvo em vez de recomeçar do topo. Quando uma aba termina naturalmente, o cursor dela é apagado. Varreduras sem `--full` ignoram cursors; para recomeçar do topo, apague o arquivo.

## Metadados

`--write-metadata` grava um sidecar `<arquivo>.mp4.json` ao lado de cada vídeo baixado, com id, URLs, data e pinned. Só no downloader nativo — no modo yt-dlp use o `--write-info-json` do próprio yt-dlp.

## yt-dlp (opcional)

O extractor de perfil do yt-dlp (`instagram:user`) está marcado como quebrado. Mesmo no modo yt-dlp, a listagem continua pela API web; o yt-dlp só baixa cada reel.

```bash
pip install -U yt-dlp
python download_instagram.py --downloader yt-dlp
```

Útil se o CDN não mandar `video_versions` (casos em que o gallery-dl cai no manifesto DASH).

## Opções úteis

```bash
python3 download_instagram.py --dry-run
python3 download_instagram.py --reels-only
python3 download_instagram.py --max 20 --retries 5
python3 download_instagram.py --full --write-metadata
python3 download_instagram.py --profiles meus-perfis.txt --out ~/Videos/ig
```

`--help` tem a lista completa (em inglês).

## Notas

- Cookies de conta logada em automação podem levar a bloqueio. Prefira uma sessão que você aceite perder.
- Não commite `cookies.txt`. O script aplica chmod 600 nele (export do browser e leitura).
- O app-id do Instagram muda de vez em quando. Se os endpoints começarem a falhar em massa, atualize com `--ig-app-id` ou a env `IG_APP_ID`.
- Se o Instagram responder login/rate-limit, atualize os cookies e aumente `--request-sleep` (que já vai com ±25% de jitter).
