# Setting up timeseries monitoring on App Engine.

1.  Symlink this directory into your appengine app.

        cd infra/appengine/myapp
        ln -s ../../appengine_module/gae_ts_mon .

1.  Add the scheduled task to your `cron.yaml` file.  Create it if you don't
    have one already.

        cron:
        - description: Send ts_mon metrics
          url: /internal/cron/ts_mon/send
          schedule: every 1 minutes
          target: <cron_module_name>  # optional

1.  Include the URL handler for that scheduled task in your `app.yaml` file.

        includes:
        - gae_ts_mon  # handles /internal/cron/ts_mon/send

1.  Initialize the library in your request handler.

        import gae_ts_mon

        [...]

        app = webapp2.WSGIApplication(my_handlers)
        gae_ts_mon.initialize(app, cron_module='<cron_module_name>')

    You must do this in every top-level request handler that's listed in your
    app.yaml to ensure metrics are registered no matter which type of request
    an instance receives first.

    If your app does not contain a `webapp2.WSGIApplication` instance
    (e.g. it's a Cloud Endpoints only app), then pass `None` as the
    first argument to `gae_ts_mon.initialize`.

    The `gae_ts_mon.initialize` method takes a few optional parameters:
     - `cron_module` (str, default='default'): if you specified a custom
       module for the cron job, you must specify it in every call to
       `initialize`.
     - `is_enabled_fn` (function with no arguments returning `bool`):
       a callback to enable/disable sending the actual metrics. Default: `None`
       which is equivalent to `lambda: True`. The callback is called on every
       metrics flush, and takes effect immediately. Make sure the callback is
       efficient, or it will slow down your requests.

1.  Instrument all Cloud Endpoint methods if you have any by adding a decorator:

        @gae_ts_mon.instrument_endpoint()
        @endpoints.method(...)
        def your_method(self, request):
          ...

1.  Give your app's service account permission to send metrics to the API.  You
    can find the name of your service account on the `Permissions` page of your
    project in the cloud console - it'll look something like
    `app-id@appspot.gserviceaccount.com`.  Or search the default module's logs
    for `"Initializing with service account"`.  Add it as a "Publisher" of the
    "monacq" PubSub topic in the
    [chrome-infra-mon-pubsub project](https://pantheon.corp.google.com/project/chrome-infra-mon-pubsub/cloudpubsub/topicList)
    by selecting it from the list and clicking "Permissions". If you see an
    error "You do not have viewing permissions for the selected resource.", then
    please ask pgervais@chromium.org (AMER) or sergiyb@chromium.org (EMEA) to do
    it for you.

1.  You also need to enable the Google Cloud Pub/Sub API for your project if
    it's not enabled already.

You're done!  You can now use ts_mon metrics exactly as you normally would using
the infra_libs.ts_mon module. Here's a quick example, but see the
[timeseries monitoring docs](https://chrome-internal.googlesource.com/infra/infra_internal/+/master/doc/ts_mon.md)
for more information.

    from infra_libs import ts_mon

    class MyHandler(webapp2.RequestHandler):
      goats_teleported = ts_mon.CounterMetric('goats/teleported')

      def get(self):
        count = goat_teleporter.teleport()
        goats_teleported.increment(count)

        self.response.write('Teleported %d goats this time' % count)


## Appengine Modules

Multiple Appengine modules are fully supported - the module name will appear in
as `job_name` field in metrics when they are exported.

The scheduled task only needs to run in one module.

## Global Metrics

Normally, each AppEnigne app's instance sends its own set of metrics
at its own pace.  This can be a problem, however, if you want to
report a metric that only makes sense globally, e.g. the count of
certain Datastore entities computed once a minute in a cron job.

Setting such a metric in an individual instance is incorrect, since a
cron job will run in a randomly selected instance, and that instance
will continue to send the same old value until it's picked by the cron
job again. The receiving monitoring endpoint will not be able to tell
which metric is the most recent, and by default will try to sum up the
values from all the instances, resulting in a wrong value.

A "global" metric is a metric that is not tied to an instance, and is
guaranteed to be computed and sent at most once per minute,
globally. Here's an example of how to set up a global metric:

    from infra_libs import ts_mon

    # Override default target fields for app-global metrics.
    TARGET_FIELDS = {
        'job_name':  '',  # module name
        'hostname': '',  # version
        'task_num':  0,  # instance ID
    }

    remaining = ts_mon.GaugeMetric('goats/remaining')
    in_flight = ts_mon.GaugeMetric('goats/in_flight)

    def set_global_metrics():
      # Query some global resource, e.g. Datastore
      remaining.set(len(goats_to_teleport()), target_fields=TARGET_FIELDS)
      in_flight.set(len(goats_being_teleported()), target_fields=TARGET_FIELDS)

    ts_mon.register_global_metrics([remaining, in_flight])
    ts_mon.register_global_metrics_callback('my callback', set_global_metrics)

The registered callback will be called at most once per minute, and
only one instance will be running it at a time. A global metric is
then cleared the moment it is sent.  Thus, global metrics will be sent
at the correct intervals, regardless of the number of instances the
app is currently running.

Note also the use of `target_fields` parameter: it overrides the
default target fields which would otherwise distinguish the metric per
module, version, or instance ID. Using `target_fields` in regular,
"local" metrics is not allowed, as it would result in errors on the
monitoring endpoint, and loss of data.
