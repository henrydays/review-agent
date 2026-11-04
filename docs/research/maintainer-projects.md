# High-stakes open source projects, measured for code review

Which projects review well enough, on GitHub or elsewhere, to learn from. Measured on 2026-11-03 by `bench/maintainers.py survey`. The corpus built from these is described in [the corpus document](../MAINTAINER_CORPUS.md).

## Why look at all

ReviewBench is a broad sample of real repositories, not of high-stakes ones. Of its 181 repositories still reachable, the median has 5 stars, the 90th percentile has 2,144, and 9 have over 10,000. Human review also barely reaches its golden set: review comments are 1.5% of its true positives, and only 20% of the comments left after its deduplication are labeled true.

An agent tuned only there is tuned for that distribution, and is graded largely against other models' opinions.

## How each project was measured

A sample of up to 72 closed pull requests: 12 from each of six one-week windows spread across a year, taken by creation date and ending six weeks before the survey. Getting that sample right took three attempts, and the mistakes are worth knowing, since each one produced a plausible table:

1. **`states: MERGED`** hid the projects that land work outside GitHub's merge button, many of them the most rigorous. OpenSSL lands with a tool and closes the PR, OpenJDK marks it `integrated`, and Spark and PyTorch close it from a commit. `landed()` now recognizes all of these.
2. **The repository's PR list, by update time,** is a slice of a few days at most. All 40 of Rails' sampled PRs came from two days, which showed Rails as never reviewing. Cockroach's came from 2017.
3. **Closed recently, newest first** favours PRs opened and closed within a day. The median Kubernetes PR in that sample was open for zero days. Filtering on creation date has no such bias.

Also, search does not follow renamed repositories: `facebook/react` is now `react/react`, and searching the old name silently finds nothing.

A maintainer here is someone with push access, a member or collaborator, or someone who commented on at least three other people's PRs in the sample. Association alone is not enough, because GitHub reports people with private organization membership as `CONTRIBUTOR`, including senior reviewers. Bots and AI reviewers do not count.

## Results

- **Reviewed**: share of human-authored landed PRs with at least one maintainer comment on a line.
- **Per PR**: median maintainer comments when there were any.
- **Changed code**: share of those comments whose lines changed afterwards, by GitHub's `isOutdated`. This is why the corpus can tell a point that landed from one that did not.
- **Reviewers**: distinct maintainers commenting in the sample.
- **Bots**: share of all review threads started by automation.
- **Usable per 100**: PRs of a reviewable size with two substantive maintainer comments, at least one of which changed code.
- **In corpus**: PRs labeled and kept, with at least one finding labeled true.

The samples are small, so read usable per 100 as a filter and not a ranking. A difference between 15 and 20 means nothing.

| Project | Domain | Language | Stars | PRs | Reviewed | Per PR | Changed code | Reviewers | Bots | Usable per 100 | In corpus | Split, or why not |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| django/django | application | Python | 91,487 | 26 | 54% | 2.5 | 73% | 6 | 0% | 30.8 | 6 | **train** |
| home-assistant/core | application | Python | 91,930 | 56 | 21% | 3 | 93% | 8 | 68% | 14.3 |  | High yield is not the only need: this domain and language were already covered by selected projects |
| rails/rails | application | Ruby | 58,843 | 62 | 10% | 1 | 75% | 3 | 0% | 0 |  | 10% reviewed and no usable PRs in the sample |
| spring-projects/spring-framework | application | Java | 60,312 | 32 | 3% | 2 | 0% | 1 | 0% | 0 |  | 3% of PRs have line comments |
| apache/arrow | data | C++ | 17,274 | 56 | 39% | 4 | 63% | 11 | 16% | 26.8 | 6 | **train** |
| apache/flink | data | Java | 26,443 | 54 | 41% | 3 | 76% | 17 | 0% | 25.9 | 6 | **holdout** |
| numpy/numpy | data | Python | 33,243 | 45 | 29% | 3 | 72% | 9 | 0% | 22.2 | 6 | **train** |
| pandas-dev/pandas | data | Python | 50,314 | 41 | 29% | 2 | 90% | 5 | 0% | 17.1 |  | High yield is not the only need: this domain and language were already covered by selected projects |
| elastic/elasticsearch | data | Java | 78,473 | 59 | 54% | 3 | 61% | 30 | 5% | 16.9 | 6 | **train** |
| scipy/scipy | data | Python | 15,180 | 58 | 29% | 2 | 70% | 8 | 0% | 13.8 |  | High yield is not the only need: this domain and language were already covered by selected projects |
| apache/spark | data | Scala | 44,393 | 58 | 26% | 3 | 68% | 10 | 0% | 13.8 |  | High yield is not the only need: this domain and language were already covered by selected projects |
| pytorch/pytorch | data | Python | 105,100 | 38 | 29% | 1 | 69% | 10 | 0% | 13.2 | 6 | **val** |
| apache/airflow | data | Python | 47,352 | 43 | 30% | 2 | 63% | 10 | 16% | 9.3 |  | High yield is not the only need: this domain and language were already covered by selected projects |
| tensorflow/tensorflow | data | C++ | 201,282 | 6 | 0% | 0 | 0% | 0 | 100% | 0 |  | 6 human PRs in the sample and none with line comments; the rest come from automation |
| redis/redis | database | C | 77,165 | 60 | 50% | 2 | 75% | 10 | 29% | 30 | 5 | **train** |
| vitessio/vitess | database | Go | 21,399 | 26 | 38% | 5 | 77% | 7 | 74% | 26.9 |  | High yield is not the only need: this domain and language were already covered by selected projects |
| valkey-io/valkey | database | C | 27,595 | 53 | 47% | 4 | 76% | 9 | 16% | 26.4 |  | A fork of Redis. Taking both would count one codebase twice |
| apache/kafka | database | Java | 34,121 | 56 | 68% | 2.5 | 62% | 18 | 6% | 25 | 6 | **holdout** |
| scylladb/scylladb | database | C++ | 15,818 | 32 | 53% | 4 | 62% | 13 | 30% | 25 | 6 | **val** |
| tikv/tikv | database | Rust | 16,936 | 38 | 40% | 2 | 56% | 16 | 60% | 18.4 | 6 | **train** |
| pingcap/tidb | database | Go | 40,705 | 56 | 32% | 2 | 62% | 11 | 54% | 17.9 |  | High yield is not the only need: this domain and language were already covered by selected projects |
| MariaDB/server | database | C++ | 8,516 | 55 | 33% | 2 | 72% | 15 | 31% | 12.7 |  | High yield is not the only need: this domain and language were already covered by selected projects |
| cockroachdb/cockroach | database | Go | 32,602 | 49 | 24% | 1 | 53% | 8 | 11% | 8.2 |  | 24% of PRs have line comments on GitHub, and few are usable |
| duckdb/duckdb | database | C++ | 42,771 | 57 | 21% | 2 | 74% | 7 | 5% | 7 |  | Too few usable PRs for the cost of collecting them |
| ClickHouse/ClickHouse | database | C++ | 50,730 | 58 | 16% | 1 | 59% | 8 | 73% | 3.4 |  | 16% reviewed, and 73% of threads are from bots |
| apache/cassandra | database | Java | 10,132 | 4 | 50% | 6.5 | 46% | 1 | 0% | 0 |  | Only 4 PRs in the sample show as landed: its commits reach the project outside the PR |
| postgres/postgres | database | C | | | | | | | | |  | Reviewed on pgsql-hackers; the GitHub repository is a read-only mirror |
| servo/servo | frontend | Rust | 38,307 | 68 | 44% | 2 | 71% | 15 | 0% | 19.1 |  | High yield is not the only need: this domain and language were already covered by selected projects |
| angular/angular | frontend | TypeScript | 101,099 | 43 | 30% | 3 | 68% | 9 | 0% | 18.6 | 6 | **train** |
| WebKit/WebKit | frontend | JavaScript | 10,306 | 68 | 32% | 2 | 63% | 21 | 0% | 14.7 |  | 12.9 GB of history, and its checkout is too large to review per PR |
| vercel/next.js | frontend | JavaScript | 143,245 | 52 | 25% | 2 | 68% | 8 | 34% | 13.5 | 6 | **val** |
| facebook/react | frontend | JavaScript | 251,891 | 31 | 16% | 2 | 91% | 3 | 0% | 9.7 | 7 | **train** |
| flutter/flutter | frontend | Dart | 181,024 | 44 | 23% | 2 | 43% | 9 | 54% | 2.3 |  | Low yield, and 54% of threads are from bots |
| vuejs/core | frontend | TypeScript | 54,821 | 58 | 3% | 1 | 100% | 1 | 82% | 0 |  | 3% reviewed, 82% of threads are from bots |
| microsoft/vscode | frontend | TypeScript | 194,530 | 54 | 11% | 1 | 25% | 4 | 86% | 0 |  | 11% reviewed, 86% of threads are from bots, no usable PRs in the sample |
| electron/electron | frontend | C++ | 123,748 | 13 | 0% | 0 | 0% | 0 | 0% | 0 |  | 13 landed PRs in the sample and no maintainer comments |
| kubernetes/kubernetes | infrastructure | Go | 128,633 | 49 | 43% | 3 | 76% | 16 | 0% | 22.4 | 10 | **train** |
| grafana/grafana | infrastructure | TypeScript | 77,656 | 47 | 49% | 2 | 70% | 23 | 42% | 21.3 |  | High yield is not the only need: this domain and language were already covered by selected projects |
| grpc/grpc | infrastructure | C++ | 45,418 | 31 | 42% | 2 | 64% | 8 | 13% | 16.1 |  | High yield is not the only need: this domain and language were already covered by selected projects |
| envoyproxy/envoy | infrastructure | C++ | 29,171 | 56 | 27% | 2 | 79% | 11 | 18% | 14.3 | 6 | **train** |
| nginx/nginx | infrastructure | C | 32,042 | 22 | 27% | 4.5 | 89% | 4 | 3% | 13.6 | 1 | **train** |
| cilium/cilium | infrastructure | Go | 25,766 | 45 | 27% | 3 | 80% | 16 | 0% | 13.3 | 6 | **val** |
| containerd/containerd | infrastructure | Go | 21,453 | 31 | 29% | 2 | 86% | 7 | 60% | 12.9 | 6 | **holdout** |
| protocolbuffers/protobuf | infrastructure | C++ | 72,150 | 11 | 18% | 1.5 | 67% | 2 | 0% | 9.1 |  | 11 landed PRs in the sample |
| prometheus/prometheus | infrastructure | Go | 66,920 | 47 | 36% | 1 | 72% | 11 | 6% | 8.5 |  | Too few usable PRs for the cost of collecting them |
| ceph/ceph | infrastructure | C++ | 17,195 | 60 | 30% | 2 | 62% | 15 | 25% | 8.3 |  | Too few usable PRs for the cost of collecting them |
| istio/istio | infrastructure | Go | 38,485 | 53 | 17% | 2 | 56% | 9 | 0% | 7.5 |  | Too few usable PRs for the cost of collecting them |
| etcd-io/etcd | infrastructure | Go | 52,442 | 33 | 21% | 2 | 76% | 4 | 0% | 6.1 |  | Too few usable PRs for the cost of collecting them |
| bazelbuild/bazel | infrastructure | Java | 26,040 | 53 | 8% | 2 | 62% | 4 | 56% | 5.7 |  | 8% of PRs have line comments, and many are labeled as imports |
| hashicorp/terraform | infrastructure | Go | 50,118 | 38 | 16% | 2.5 | 65% | 5 | 0% | 5.3 |  | Too few usable PRs for the cost of collecting them |
| moby/moby | infrastructure | Go | 72,241 | 39 | 18% | 1 | 86% | 3 | 55% | 5.1 |  | Too few usable PRs for the cost of collecting them |
| zephyrproject-rtos/zephyr | kernel | C | 16,984 | 55 | 34% | 2 | 84% | 25 | 9% | 14.5 | 4 | **val** |
| systemd/systemd | kernel | C | 16,992 | 60 | 30% | 2 | 72% | 8 | 76% | 10 | 6 | **train** |
| openzfs/zfs | kernel | C | 12,539 | 65 | 25% | 1 | 79% | 4 | 28% | 6.2 |  | Too few usable PRs for the cost of collecting them |
| torvalds/linux | kernel | C | | | | | | | | | 23 | **train** |
| qemu/qemu | kernel | C | | | | | | | | |  | Reviewed on a mailing list. The Linux adapter could be pointed at it, not done yet |
| php/php-src | language | C | 41,755 | 62 | 37% | 2 | 80% | 10 | 0% | 22.6 | 4 | **train** |
| tokio-rs/tokio | language | Rust | 33,567 | 39 | 36% | 2 | 81% | 6 | 0% | 20.5 | 6 | **train** |
| llvm/llvm-project | language | LLVM | 41,311 | 65 | 42% | 3 | 76% | 30 | 2% | 18.5 | 6 | **holdout** |
| rust-lang/rust | language | Rust | 121,009 | 51 | 29% | 2 | 69% | 17 | 0% | 15.7 | 6 | **holdout** |
| openjdk/jdk | language | Java | 23,495 | 67 | 39% | 1 | 71% | 19 | 0% | 11.9 | 6 | **train** |
| swiftlang/swift | language | Swift | 70,525 | 62 | 21% | 3 | 65% | 12 | 0% | 11.3 |  | High yield is not the only need: this domain and language were already covered by selected projects |
| dotnet/runtime | language | C# | 18,403 | 45 | 36% | 2 | 75% | 16 | 61% | 11.1 |  | Same organization as ReviewBench PRs, which would break corpus separation |
| bytecodealliance/wasmtime | language | Rust | 18,820 | 64 | 31% | 2.5 | 72% | 7 | 3% | 9.4 | 6 | **train** |
| denoland/deno | language | Rust | 108,793 | 43 | 23% | 2 | 64% | 2 | 33% | 9.3 |  | High yield is not the only need: this domain and language were already covered by selected projects |
| nodejs/node | language | JavaScript | 123,205 | 35 | 34% | 1 | 61% | 11 | 0% | 8.6 | 6 | **val** |
| JuliaLang/julia | language | Julia | 49,237 | 59 | 27% | 1 | 68% | 8 | 0% | 6.8 |  | Too few usable PRs for the cost of collecting them |
| ruby/ruby | language | Ruby | 23,795 | 63 | 27% | 1 | 82% | 7 | 26% | 6.3 |  | Too few usable PRs for the cost of collecting them |
| python/cpython | language | Python | 77,802 | 64 | 16% | 1.5 | 83% | 13 | 0% | 6.2 |  | Too few usable PRs for the cost of collecting them |
| dotnet/roslyn | language | C# | 20,759 | 44 | 32% | 2.5 | 35% | 11 | 39% | 4.5 |  | Same organization as ReviewBench PRs, which would break corpus separation |
| microsoft/TypeScript | language | Go | 111,726 | 14 | 7% | 1 | 100% | 1 | 60% | 0 |  | 7% reviewed, 14 landed PRs in the sample |
| golang/go | language | Go | | | | | | | | |  | Reviewed on Gerrit, whose API has per-line comments and patch sets. A good next adapter |
| git/git | language | C | | | | | | | | |  | Reviewed on a mailing list |
| Mbed-TLS/mbedtls | security | C | 7,050 | 22 | 55% | 4 | 81% | 6 | 0% | 36.4 | 6 | **train** |
| bitcoin/bitcoin | security | C++ | 90,594 | 47 | 53% | 3 | 69% | 13 | 1% | 29.8 | 6 | **train** |
| tailscale/tailscale | security | Go | 38,573 | 59 | 51% | 2 | 79% | 21 | 9% | 25.4 | 6 | **train** |
| aws/s2n-tls | security | C | 4,784 | 47 | 53% | 2 | 67% | 8 | 0% | 23.4 | 6 | **val** |
| cloudflare/quiche | security | Rust | 12,843 | 29 | 28% | 3 | 58% | 6 | 0% | 17.2 | 5 | **holdout** |
| letsencrypt/boulder | security | Go | 5,778 | 35 | 31% | 3 | 60% | 4 | 7% | 17.1 | 6 | **holdout** |
| openssl/openssl | security | C | 31,176 | 53 | 53% | 1.5 | 68% | 15 | 2% | 17 | 6 | **train** |
| sigstore/cosign | security | Go | 6,405 | 24 | 25% | 2.5 | 50% | 3 | 0% | 8.3 |  | Too few usable PRs for the cost of collecting them |
| ethereum/go-ethereum | security | Go | 51,459 | 41 | 24% | 1 | 60% | 5 | 0% | 4.9 |  | Too few usable PRs for the cost of collecting them |
| curl/curl | security | C | 43,362 | 53 | 8% | 1 | 60% | 2 | 84% | 1.9 |  | 8% reviewed, and 84% of threads are from bots |
| hashicorp/vault | security | Go | 36,462 | 3 | 0% | 0 | 0% | 0 | 0% | 0 |  | 3 landed PRs in the sample and no maintainer comments, so review is not visible here |
| signalapp/libsignal | security | Rust | 6,112 | 0 | 0% | 0 | 0% | 0 | 0% | 0 |  | No landed PRs in the sample: the repository is a publishing mirror |

## What stands out

- **Line comments do not follow fame.** Over half of the PRs at Mbed TLS, Django, Bitcoin and Redis have them, against 16% at React and CPython. This measures comments on GitHub and not care: a project can review closely with fewer comments, or have many small PRs that need none.
- **Some of the most important projects cannot be collected from GitHub at all.** Linux, PostgreSQL, Git and QEMU review on mailing lists, and Go on Gerrit. TensorFlow, protobuf, Vault and libsignal show too little on GitHub to use. An empty row here says the review is not visible on GitHub, not that there is none.
- **Force pushes hide the evidence from one API.** GitHub's GraphQL API reports no original commit for a comment once a force push has replaced it, which affects nginx, where PRs see 19 to 27 force pushes each, along with Zephyr and Bitcoin. REST still has it. Using GraphQL alone would have dropped careful projects for being careful.

## Worth adding next

- **Go, through Gerrit.** Its API has per-line comments, explicit patch sets and an unresolved flag, which is cleaner than either source here.
- **QEMU, or other subsystems of Linux.** `bench/lore.py` works on any list on lore.kernel.org.
- **Bugs that escaped review.** The kernel's `Fixes:` tag names the commit that introduced a bug, and the fix's message explains it. That gives high-severity ground truth that no reviewer caught, which human comments rarely provide.
