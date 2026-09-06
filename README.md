# yt-down

Baixa vídeos de perfis do Instagram. Na segunda execução, só puxa o que ainda não está no disco nem no `data/archive.txt`.

Por padrão **não usa yt-dlp**: a listagem já traz `video_versions` (MP4 no CDN do Instagram), o mesmo campo que o [gallery-dl](https://github.com/mikf/gallery-dl) usa. O download é HTTP com a biblioteca padrão.

yt-dlp continua opcional (`--downloader yt-dlp`) e também serve só para exportar cookies do browser.

## Setup

Python 3.10+ (no macOS o `python3` do Xcode pode ser 3.9 — use o do Homebrew).

Não precisa instalar nada além do Python, se você já tiver um `cookies.txt` Netscape.

O Instagram exige sessão logada. Duas formas:

1. Exporte `cookies.txt` (extensão tipo *Get cookies.txt LOCALLY*) e coloque na raiz do projeto.
2. Ou, se o yt-dlp estiver instalado:

```bash
python download_instagram.py --cookies-from-browser chrome
```

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

Ao terminar, o script imprime `downloaded / skipped / failed` e sai com código `1` se algum download falhou. 429 e erros de rede tentam de novo (padrão: 3 retries).

## yt-dlp (opcional)

O extractor de perfil do yt-dlp (`instagram:user`) está marcado como quebrado. Mesmo no modo yt-dlp, a listagem continua pela API web; o yt-dlp só baixa cada reel.

```bash
pip install -U yt-dlp
python download_instagram.py --downloader yt-dlp
```

Útil se o CDN não mandar `video_versions` (casos em que o gallery-dl cai no manifesto DASH).

## Opções úteis

```bash
python download_instagram.py --dry-run
python download_instagram.py --reels-only
python download_instagram.py --max 20 --retries 5
python download_instagram.py --profiles meus-perfis.txt --out ~/Videos/ig
```

`--help` tem a lista completa (em inglês).

## Notas

- Cookies de conta logada em automação podem levar a bloqueio. Prefira uma sessão que você aceite perder.
- Não commite `cookies.txt`.
- Se o Instagram responder login/rate-limit, atualize os cookies e aumente `--request-sleep`.
