require 'json'
require 'open3'

module GamePatcher
  ROOT = File.expand_path('.')
  PLUGIN_DIR = File.expand_path(PluginManager.findDirectory('Game Patcher'))
  SCRIPT = File.join(PLUGIN_DIR, 'runtime', 'updater.py')
  CONFIG_PATH = File.join(PLUGIN_DIR, 'settings.json')
  def self.decode_json(value)
    value = value.dup.force_encoding(Encoding::UTF_8)
    parser = JSON::Parser
    parser.new(value).parse
  end

  CONFIG = decode_json(File.binread(CONFIG_PATH))
  WINDOWS = RUBY_PLATFORM =~ /mswin|mingw|cygwin/i

  class << self
    attr_reader :result

    def executable(name)
      names = WINDOWS && File.extname(name).empty? ? [name + '.exe', name] : [name]
      ENV.fetch('PATH', '').split(File::PATH_SEPARATOR).each do |dir|
        names.each do |filename|
          path = File.join(dir, filename)
          return path if File.file?(path) && File.executable?(path)
        end
      end
      nil
    end

    def python_command(check = false)
      if WINDOWS
        bundled = File.join(PLUGIN_DIR, 'bin', 'windows', check ? 'pythonw.exe' : 'python.exe')
        return [bundled, '-I', '-X', 'utf8'] if File.file?(bundled)
        python = executable('python') || executable('python3')
      else
        python = executable('python3')
      end
      raise 'Python runtime is missing.' if !python
      [python, '-I', '-X', 'utf8']
    end

    def start_check
      return if @started || CONFIG['auto_check'] == false
      @started = true
      @notified = false
      @result = nil
      @worker = Thread.new do
        begin
          output, status = Open3.capture2e(*python_command(true), SCRIPT, 'check', '--root', ROOT, '--config', CONFIG_PATH)
          @result = decode_json(output)
        rescue StandardError => e
          @result = { 'status' => 'error', 'message' => e.message }
        end
      end
    end

    def notify(visuals)
      return if @notified || !@result || @result['status'] != 'available'
      # Message rendering re-enters this update method.
      @notified = true
      version = @result['version']
      panel = visuals.sprites[:game_patcher_update] if visuals.respond_to?(:sprites)
      if panel
        panel.label = "Update (#{version})"
        panel.refresh
      end
      pbMessage("Update #{version} is available. Select Update in the main menu to install it.")
    end

    def terminal_command
      runner = [*python_command, SCRIPT, WINDOWS ? 'launch' : 'install', '--root', ROOT, '--config', CONFIG_PATH, '--wait-pid', Process.pid.to_s]
      return runner if WINDOWS
      [
        ['konsole', ['-e']],
        ['gnome-terminal', ['--wait', '--']],
        ['xfce4-terminal', ['--disable-server', '--execute']],
        ['kitty', []],
        ['xterm', ['-e']]
      ].each do |name, flags|
        terminal = executable(name)
        return [terminal, *flags, *runner] if terminal
      end
      raise 'No supported terminal found (for example Konsole or xterm).'
    end

    def install(screen)
      command = terminal_command
      prompt = 'Close the game and start updates in a terminal?'
      return if !screen.show_confirm_message(prompt)
      if WINDOWS
        _output, status = Open3.capture2e(*command)
        raise 'Could not open the Windows console.' if !status.success?
      else
        pid = Process.spawn(*command, :chdir => ROOT)
        Process.detach(pid)
      end
      screen.end_screen
      $scene = nil
      exit
    rescue StandardError => e
      screen.show_message("Could not start update: #{e.message}")
    end
  end
end
