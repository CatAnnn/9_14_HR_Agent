import {
  ArrowLeft,
  ArrowRight,
  BookOpen,
  Download,
  LoaderCircle,
} from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { downloadEbook, recordEbookEvent } from '../api/ebooks';
import { LandingHeader } from '../components/LandingExperience';
import {
  ebookCatalog,
  ebookDetailPath,
  ebookPublishedLabel,
  ebookReaderPath,
  findEbook,
} from '../content/ebook-content';
import { homeExperienceContent } from '../content/home-experience-content';
import { resourceBookPageContent } from '../content/resource-book-content';
import { useLanguage } from '../i18n/LanguageContext';
import { useAuthStore } from '../store/authStore';
import { userFacingErrorMessage } from '../utils/displayText';
import '../styles/home-page.css';
import '../styles/ebook-pages.css';

type DownloadErrorState = {
  code: 'ebook_download_failed';
  cause: unknown;
};


export default function EbookDetailPage() {
  const { bookId } = useParams();
  const book = findEbook(bookId);
  const { user } = useAuthStore();
  const { translate, translateTemplate } = useLanguage();
  const navigate = useNavigate();
  const location = useLocation();
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<DownloadErrorState | null>(null);
  const bookMetadata = book
    ? resourceBookPageContent.books.items.find((item) => item.id === book.id)
    : undefined;

  useEffect(() => {
    document.title = book
      ? translateTemplate(
        '{title} | Performance Feedback',
        '{title} | Performance Feedback',
        { title: translate(book.title, bookMetadata?.titleEnglish) },
      )
      : translate('未找到书籍');
  }, [book, bookMetadata?.titleEnglish, translate, translateTemplate]);

  if (!book) {
    return (
      <main className="ebook-detail-page ebook-not-found">
        <LandingHeader
          navigation={homeExperienceContent.navigation}
          homeHref="/"
          anchorBasePath="/"
        />
        <section>
          <BookOpen aria-hidden="true" />
          <h1>{translate('未找到这本电子书')}</h1>
          <p>{translate('书目可能已经更新，请返回资源中心重新选择。')}</p>
          <Link to="/resources">{translate('返回资源中心')}</Link>
        </section>
      </main>
    );
  }

  const readPath = ebookReaderPath(book.id);
  const relatedBooks = ebookCatalog.filter(({ id }) => id !== book.id).slice(0, 3);

  const requireLogin = () => {
    navigate('/login', { state: { from: `${location.pathname}${location.search}` } });
  };

  const handleDownload = async () => {
    if (!user) {
      requireLogin();
      return;
    }
    setDownloading(true);
    setDownloadError(null);
    try {
      const artifact = await downloadEbook(book.id);
      const url = URL.createObjectURL(artifact.blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = artifact.filename;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      void recordEbookEvent(book.id, 'download').catch(() => undefined);
    } catch (error) {
      setDownloadError({ code: 'ebook_download_failed', cause: error });
    } finally {
      setDownloading(false);
    }
  };

  const downloadErrorMessage = downloadError?.code === 'ebook_download_failed'
    ? userFacingErrorMessage(downloadError.cause, undefined, translate('电子书下载失败。'))
    : '';

  return (
    <main className="ebook-detail-page">
      <LandingHeader
        navigation={homeExperienceContent.navigation}
        homeHref="/"
        anchorBasePath="/"
      />

      <div className="ebook-detail-shell">
        <Link className="ebook-back-link" to="/resources">
          <ArrowLeft aria-hidden="true" />
          <span>{translate('返回资源中心')}</span>
        </Link>

        <article className="ebook-detail-hero">
          <div className="ebook-detail-cover">
            <img
              src={book.cover}
              alt={translateTemplate('{title}封面', '{title} cover', { title: book.title })}
              width="600"
              height="800"
            />
          </div>
          <div className="ebook-detail-copy">
            <p className="ebook-detail-eyebrow">{translate('在线书库')}</p>
            <h1>{book.title}</h1>
            <p className="ebook-detail-authors">{book.authors.join('、')}</p>
            <dl className="ebook-detail-metadata">
              {book.publisher && <><dt>{translate('出版社')}</dt><dd>{book.publisher}</dd></>}
              {book.publishedAt && (
                <><dt>{translate('出版日期')}</dt><dd>{ebookPublishedLabel(book.publishedAt)}</dd></>
              )}
            </dl>
            <p className="ebook-detail-summary">{book.summary}</p>
            <ul className="ebook-detail-tags" aria-label={translate('主题标签')}>
              {book.tags.map((tag) => <li key={tag}>{tag}</li>)}
            </ul>
            <div className="ebook-detail-actions">
              <Link className="ebook-primary-action" to={readPath}>
                <BookOpen aria-hidden="true" />
                <span>{translate(user ? '在线阅读' : '登录后阅读')}</span>
              </Link>
              <button type="button" onClick={() => void handleDownload()} disabled={downloading}>
                {downloading
                  ? <LoaderCircle className="spin" aria-hidden="true" />
                  : <Download aria-hidden="true" />}
                <span>{translate(user ? '下载 EPUB' : '登录后下载')}</span>
              </button>
            </div>
            {downloadErrorMessage && (
              <p className="ebook-action-error" role="alert">{downloadErrorMessage}</p>
            )}
            {!user && <p className="ebook-auth-note">{translate('封面与简介可公开浏览，正文阅读和下载需要登录。')}</p>}
          </div>
        </article>

        <section className="ebook-related" aria-labelledby="ebook-related-title">
          <header>
            <p>{translate('继续阅读')}</p>
            <h2 id="ebook-related-title">{translate('更多沟通与发展书籍')}</h2>
          </header>
          <div>
            {relatedBooks.map((item) => (
              <Link to={ebookDetailPath(item.id)} key={item.id}>
                <img src={item.cover} alt="" loading="lazy" width="300" height="400" />
                <span><strong>{item.title}</strong><small>{item.authors.join('、')}</small></span>
                <ArrowRight aria-hidden="true" />
              </Link>
            ))}
          </div>
        </section>
      </div>
    </main>
  );
}
