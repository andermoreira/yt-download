# yt-down

Baixa vídeos do Instagram. Na segunda execução, só puxa o que ainda não está no disco nem no `data/archive.txt`.

Há dois passos separados, no mesmo espírito do gallery-dl (extractor vs downloader) e do `yt-dlp --skip-download`:

1. **Descobrir** — perfil → fila JSONL (`data/queue.jsonl`), sem baixar MP4.
2. **Baixar** — fila → `media/info` + CDN. URLs de CDN **não** entram na fila (expiram).

Por padrão **não usa yt-dlp**: o download nativo pede o MP4 no CDN. yt-dlp continua opcional (`--downloader yt-dlp`) e também serve só para exportar cookies do browser.

A listagem REST de perfil (`clips/user`, `feed/user`) frequentemente responde **429**. O discover usa GraphQL da web (o mesmo caminho do instaloader) e cai no REST se o GraphQL falhar. `--from-queue` pede o MP4 via GraphQL primeiro; REST `media/info` é fallback quando o GraphQL falha por motivo que não seja throttle.

## Setup

Python 3.10+ (no macOS o `python3` do Xcode pode ser 3.9 — use o do Homebrew: `export PATH="/opt/homebrew/bin:$PATH"`).

Não precisa instalar nada além do Python, se você já tiver um `cookies.txt` Netscape.

O Instagram exige sessão logada. Duas formas:

1. Exporte `cookies.txt` (extensão tipo *Get cookies.txt LOCALLY*) e coloque na raiz do projeto.
2. Ou, se o yt-dlp estiver instalado (Chrome fechado; perfil Default = `chrome`, outro = `chrome:Profile 1`):

```bash
python3 download_instagram.py --cookies-from-browser chrome
```

O dump **não** acessa o Instagram (o extractor do yt-dlp está quebrado e essa request pode invalidar a sessão). Sem `--from-queue` nem `--discover-only` (e sem URL de reel/post em `profiles.txt`), o comando **só** grava `cookies.txt` e sai. O User-Agent padrão é Chrome 152 — se o browser for outra major, passe `--user-agent` ou `IG_USER_AGENT`.

## Uso

Copie `profiles.txt.example` para `profiles.txt` e edite — um item por linha. `profiles.txt` não vai no git.

### Reel ou post (sem listar perfil)

```bash
python3 download_instagram.py --profiles profiles.txt --max 1 --request-sleep 2
```

Se `profiles.txt` tiver username ou URL de perfil, o script **pula** com warning. Listagem de perfil não roda no caminho padrão (evita 429 longo).

### Descobrir perfil → fila

```bash
python3 download_instagram.py --discover-only --max 20
```

Comece com `--max` baixo. `--max` limita **linhas novas** na fila. 429 no meio do caminho deixa o JSONL parcial. `--stop-after-existing` continua valendo. Reel/post em `profiles.txt` entra na fila **sem** chamar `media/info`, mesmo se o ID já estiver no archive — quem pula o MP4 é o `--from-queue`.

Não rode o caminho padrão (`python3 download_instagram.py` sem flags) no `profiles.txt` de usernames: isso **pula** perfil. Use `--discover-only` e depois `--from-queue`.

### Baixar da fila

```bash
python3 download_instagram.py --from-queue --dry-run --max 1
python3 download_instagram.py --from-queue --max 5
```

`--discover-only` e `--from-queue` são mutuamente exclusivos. Fila padrão: `data/queue.jsonl` (`--queue` troca o path).

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

Em `--discover-only`, reels e feed são varridos **separados**. Depois de 3 vídeos seguidos que você já tem **naquela aba**, a varredura da aba para — pinned posts não entram nessa conta. Para varrer tudo:

```bash
python3 download_instagram.py --discover-only --full
```

`--full` salva a posição da paginação em `data/cursors.json` depois de cada página. Se a varredura for interrompida (Ctrl+C sai com código 130, `--max`, crash), a próxima `--full` do mesmo perfil retoma do ponto salvo. Quando uma aba termina naturalmente, o cursor dela é apagado. Varreduras sem `--full` ignoram cursors; para recomeçar do topo, apague o arquivo.

Ao terminar, o script imprime `downloaded / skipped / failed` (e `listed` quando enfileirou ou fez dry-run) e sai com código `1` se algum download falhou. 429, 401 *please wait*, 403 e erros de rede tentam de novo (padrão: 3 retries). `--max` conta só downloads (ou linhas novas na fila) com sucesso — falha não entra na conta. Linhas inválidas em `profiles.txt` ou na fila são puladas com warning — não abortam a execução. Throttle persistente (*please wait*) **para a fila** para não martelar o limite; espere alguns minutos e rode de novo.

## Metadados

`--write-metadata` grava um sidecar `<arquivo>.mp4.json` ao lado de cada vídeo baixado, com id, URLs, data e pinned. Só no downloader nativo — no modo yt-dlp use o `--write-info-json` do próprio yt-dlp.

## yt-dlp (opcional)

O extractor de perfil do yt-dlp (`instagram:user`) está marcado como quebrado. Mesmo no modo yt-dlp, a listagem de perfil (quando você usa `--discover-only`) continua pela API web; o yt-dlp só baixa cada reel.

```bash
pip install -U yt-dlp
python3 download_instagram.py --from-queue --downloader yt-dlp
```

Útil se o CDN não mandar `video_versions` (casos em que o gallery-dl cai no manifesto DASH).

## Opções úteis

```bash
python3 download_instagram.py --dry-run --profiles um-reel.txt
python3 download_instagram.py --discover-only --reels-only --max 20
python3 download_instagram.py --from-queue --max 20 --retries 5
python3 download_instagram.py --from-queue --write-metadata
python3 download_instagram.py --profiles meus-perfis.txt --out ~/Videos/ig
```

`--help` tem a lista completa (em inglês).

## Notas

- Cookies de conta logada em automação podem levar a bloqueio. Prefira uma sessão que você aceite perder.
- Não commite `cookies.txt`. O script aplica chmod 600 nele (export do browser e leitura).
- O app-id do Instagram muda de vez em quando. Se os endpoints começarem a falhar em massa, atualize com `--ig-app-id` ou a env `IG_APP_ID`.
- `"Please wait a few minutes"` é throttle: o script retenta e, se persistir, **para**. Espere de verdade alguns minutos (não só os retries de 8–32s) e rode `--from-queue` de novo. Aumente `--request-sleep` se o limite voltar rápido.
- Redirect para `/accounts/login` é sessão morta. Exporte cookies de novo (`--cookies-from-browser chrome`, Chrome fechado).
- `--discover-only` em `profiles.txt` de usernames usa GraphQL para listar. Se o Meta rotacionar os `doc_id`, a listagem pode quebrar até atualizar o script.
