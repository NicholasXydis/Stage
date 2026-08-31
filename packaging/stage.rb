class Stage < Formula
  include Language::Python::Virtualenv

  desc "Aggregates CS internship postings (Canada/US, EN + FR) into a local SQLite database"
  homepage "https://github.com/NicholasXydis/Stage"
  url "PLACEHOLDER_SDIST_URL"
  sha256 "PLACEHOLDER_SDIST_SHA256"
  license "MIT"

  depends_on "python@3.12"

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/stage --version")
    assert_match "Usage:", shell_output("#{bin}/stage --help")
  end
end
