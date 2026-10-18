package agenthub

import (
	"context"
	"io"
)

// export_test.go は unexported な内部関数をテストパッケージへ公開する
// (白箱テスト用。ビルドタグ無しの _test.go なので配布物には含まれない)。

// ErrBodySnippet は errBodySnippet のテスト用エイリアス。
func ErrBodySnippet(r io.Reader) string { return errBodySnippet(r) }

// RunSSELoopForTest は runSSELoop を直接叩くためのテスト用フック。
// handleSSEStream が runSSELoop のエラーを握り潰すため、SSE GET のエラー経路は
// これ以外に観測できない。
func (c *Client) RunSSELoopForTest(ctx context.Context, sid string) error {
	return c.runSSELoop(ctx, sid)
}
